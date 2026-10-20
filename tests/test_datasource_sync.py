"""Sync engine: materialize external tables locally + register external catalog
rows. A SQLite 'source' is synced into a SQLite 'app store'."""
import json
import logging
import re
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect, MetaData, Table, Column, String, Integer, Float, insert, text
from sqlalchemy.exc import SQLAlchemyError

import core.database as database
from core.database import Base, utcnow_naive
import core.querybuilder_models as qm
import src.datasource_connectors as dc
import src.datasource_sync as sync


@pytest.fixture
def source_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'src.db'}")
    md = MetaData()
    customers = Table("customers", md,
                      Column("id", String, primary_key=True), Column("name", String))
    md.create_all(eng)
    with eng.begin() as c:
        c.execute(insert(customers), [{"id": "c1", "name": "Acme"},
                                      {"id": "c2", "name": "Globex"}])
    return eng


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'app.db'}")
    Base.metadata.create_all(bind=eng)
    from sqlalchemy.orm import sessionmaker
    return eng, sessionmaker(bind=eng)()


def test_sync_materializes_and_registers(monkeypatch, source_engine, app_db):
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds); db.commit()

    # Point the connector at our SQLite "source" instead of a live Postgres.
    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return source_engine
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    result = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)
    assert result["synced"] == ["customers"]
    assert result["rows"]["customers"] == 2

    # External catalog row registered (is_internal False) with its fields.
    # Replace mode (QB-05) stages into a uniquely-named table and repoints
    # physical_table_name to it -- it is no longer the deterministic `phys`
    # name itself, but the deterministic name IS its prefix.
    ct = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).one()
    phys = sync.physical_name(ds.id, "customers")
    assert ct.is_internal is False
    assert ct.physical_table_name != phys
    assert ct.physical_table_name.startswith(phys + "__")
    fields = {f.name for f in db.query(qm.CatalogField).filter(qm.CatalogField.table_id == ct.id)}
    assert {"id", "name"} <= fields

    # Local materialized table (the staging table the pointer now targets)
    # exists with the rows.
    with app_engine.connect() as c:
        names = {r[0] for r in c.execute(text(f'SELECT name FROM "{ct.physical_table_name}" ORDER BY name'))}
    assert names == {"Acme", "Globex"}

    # Idempotent: a second sync refreshes rather than duplicating catalog rows.
    sync.sync_source(db, ds, ["customers"], app_engine=app_engine)
    assert db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).count() == 1


def _ds_with_failing_connector(monkeypatch, db, app_db, message):
    """Register a DataSource whose connector raises `message` (str) from
    fetch_table for every table, and patch sync.get_connector to return it.

    Raises dc.ConnectorError (not a bare RuntimeError) to match what the real
    connector layer raises on failure (src/datasource_connectors.py) -- this
    keeps the fixture caught by the narrowed
    `except (ConnectorError, SQLAlchemyError)` at datasource_sync.py L142."""
    app_engine, _ = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _FailingShim(dc.DataSourceConnector):
        def make_engine(self):
            raise AssertionError("should not be called")

        def fetch_table(self, name: str):
            raise dc.ConnectorError(message)

    monkeypatch.setattr(sync, "get_connector", lambda _ds: _FailingShim())
    return ds


def test_scrub_errors_dict(monkeypatch, app_db):
    app_engine, db = app_db
    ds = _ds_with_failing_connector(
        monkeypatch, db, app_db,
        "connection failed: postgresql://u:secretpw@h/d",
    )

    result = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    assert "secretpw" not in result["errors"]["customers"]
    assert "h" in result["errors"]["customers"]
    assert "d" in result["errors"]["customers"]


def test_scrub_log(monkeypatch, app_db, caplog):
    app_engine, db = app_db
    ds = _ds_with_failing_connector(
        monkeypatch, db, app_db,
        "connection failed: postgresql://u:secretpw@h/d",
    )

    with caplog.at_level("WARNING"):
        sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    warning_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretpw" not in warning_text


def test_last_sync_error_clean(monkeypatch, app_db):
    app_engine, db = app_db
    ds = _ds_with_failing_connector(
        monkeypatch, db, app_db,
        "connection failed: postgresql://u:secretpw@h/d",
    )

    sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    assert "secretpw" not in (ds.last_sync_error or "")


def test_run_due_syncs_scrubs_exception_log(monkeypatch, app_db, caplog):
    """WR-03: run_due_syncs must scrub credentials from any exception that
    propagates out of sync_source (e.g. a driver error during connector/engine
    construction, or a DB-commit failure -- both happen outside sync_source's
    own per-table try/except) before writing it to server.log. Every other
    D-08 sink already routes through scrub_dsn; this was the last unscrubbed
    one.

    Raises dc.ConnectorError (not a bare RuntimeError) so this stays caught by
    the narrowed `except (ConnectorError, SQLAlchemyError)` at
    datasource_sync.py L177 -- matching a real driver/connector construction
    failure."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d",
                       username="u", enabled=True,
                       options=json.dumps({"sync": {"enabled": True, "interval_minutes": 60,
                                                    "tables": ["customers"]}}))
    db.add(ds)
    db.commit()

    def _raise(*a, **k):
        raise dc.ConnectorError("connection failed: postgresql://u:secretpw@h/d")
    monkeypatch.setattr(sync, "sync_source", _raise)

    with caplog.at_level("WARNING"):
        done = sync.run_due_syncs(db, app_engine=app_engine)

    assert done == []  # the failing source is not reported as synced
    warning_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretpw" not in warning_text
    assert "scheduled sync" in warning_text


def test_happy_path_unchanged(monkeypatch, source_engine, app_db):
    """Success path (out['synced']/out['rows']) is unaffected by the scrub change."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return source_engine
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    result = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)
    assert result["synced"] == ["customers"]
    assert result["rows"]["customers"] == 2
    assert result["errors"] == {}
    assert ds.last_sync_error is None


# --- SEC-05/D-10 regression: narrowing the per-table/per-source except to
# (ConnectorError, SQLAlchemyError) must not break the "one bad table/source
# must not abort the rest" continuation, and scrub_dsn must still run. These
# assertions pass BOTH before and after the narrowing lands (the current bare
# `except Exception` already catches ConnectorError/SQLAlchemyError) -- their
# job is to LOCK the behavior so the narrowing task cannot regress it.

class _MixedFetchShim(dc.DataSourceConnector):
    """A connector whose fetch_table raises `exc` for `fail_table` and
    otherwise delegates to the real base-class implementation (reflect +
    stream rows) against `engine`."""

    def __init__(self, engine, fail_table, exc):
        self._engine = engine
        self._fail_table = fail_table
        self._exc = exc

    def make_engine(self):
        return self._engine

    def fetch_table(self, name: str):
        if name == self._fail_table:
            raise self._exc
        return super().fetch_table(name)


@pytest.mark.parametrize("exc", [
    dc.ConnectorError("connection failed: postgresql://u:secretpw@h/d"),
    SQLAlchemyError("connection failed: postgresql://u:secretpw@h/d"),
], ids=["ConnectorError", "SQLAlchemyError"])
def test_sync_source_continues_after_typed_table_failure(monkeypatch, source_engine, app_db, caplog, exc):
    """First table fails with a typed exception; the loop must still sync the
    second table, and both the errors dict and the log must be scrubbed."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    monkeypatch.setattr(
        sync, "get_connector",
        lambda _ds: _MixedFetchShim(source_engine, "bad_table", exc),
    )

    with caplog.at_level("WARNING"):
        result = sync.sync_source(db, ds, ["bad_table", "customers"], app_engine=app_engine)

    # Failed table: scrubbed error recorded, loop did not abort.
    assert "secretpw" not in result["errors"]["bad_table"]
    assert "h" in result["errors"]["bad_table"]
    # Second table: synced normally despite the first table's failure.
    assert result["synced"] == ["customers"]
    assert result["rows"]["customers"] == 2

    warning_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretpw" not in warning_text
    assert any("sync of" in r.getMessage() and "bad_table" in r.getMessage()
               for r in caplog.records)


def test_run_due_syncs_continues_after_typed_source_failure(monkeypatch, app_db, caplog):
    """One due source's sync_source raises a typed exception; the remaining
    due sources must still be synced, and the log must be scrubbed."""
    app_engine, db = app_db
    bad = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="bad-src",
                        source_type="postgres", host="x", port=1, database="d",
                        username="u", enabled=True,
                        options=json.dumps({"sync": {"enabled": True, "interval_minutes": 60,
                                                     "tables": ["t"]}}))
    good = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="good-src",
                         source_type="postgres", host="x", port=1, database="d",
                         username="u", enabled=True,
                         options=json.dumps({"sync": {"enabled": True, "interval_minutes": 60,
                                                      "tables": ["t"]}}))
    db.add(bad)
    db.add(good)
    db.commit()

    def _fake_sync_source(db_, ds, tables, app_engine=None):
        if ds.id == bad.id:
            raise dc.ConnectorError("connection failed: postgresql://u:secretpw@h/d")
        return {"synced": tables, "rows": {}, "errors": {}}

    monkeypatch.setattr(sync, "sync_source", _fake_sync_source)

    with caplog.at_level("WARNING"):
        done = sync.run_due_syncs(db, app_engine=app_engine)

    assert bad.id not in done
    assert good.id in done  # the remaining due source still synced

    warning_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretpw" not in warning_text
    assert "scheduled sync" in warning_text


# --- WR-04/SEC-05 gap-closure regression: 14-03 narrowed both per-loop excepts
# to (ConnectorError, SQLAlchemyError). An UNTYPED exception (e.g. the bare
# ValueError materialize_synctable raises when upsert mode is configured
# without a valid key) must still be caught, scrubbed, and must not abort the
# loop. These three tests are RED until the Task-2 trailing
# `except Exception` fallback lands at both loop sites.

def test_sync_source_continues_on_real_upsert_keyerror(monkeypatch, tmp_path, app_db):
    """Concrete reachable case: upsert mode configured without a valid key for
    the first table -> materialize_synctable raises a real (untyped)
    ValueError. The per-table loop must catch it, record it in
    out['errors'], and continue to sync the second table."""
    app_engine, db = app_db
    eng = create_engine(f"sqlite:///{tmp_path/'src_upsert.db'}")
    md = MetaData()
    accounts = Table("accounts", md,
                      Column("id", String, primary_key=True), Column("name", String))
    orders = Table("orders", md,
                   Column("id", String, primary_key=True), Column("total", String))
    md.create_all(eng)
    with eng.begin() as c:
        c.execute(insert(accounts), [{"id": "a1", "name": "Acme"}])
        c.execute(insert(orders), [{"id": "o1", "total": "9.99"}])

    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u",
                       options=json.dumps({"sync": {"mode": "upsert", "keys": {"orders": "id"}}}))
    db.add(ds)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return eng
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    # "accounts" has no entry in sync.keys -> materialize_synctable's
    # upsert-without-key guard raises a bare ValueError for it; "orders" is
    # mapped to a real key column and must still sync.
    result = sync.sync_source(db, ds, ["accounts", "orders"], app_engine=app_engine)

    assert "accounts" in result["errors"]
    assert "orders" in result["synced"]
    assert result["rows"]["orders"] == 1


def test_sync_source_continues_and_scrubs_untyped(monkeypatch, source_engine, app_db, caplog):
    """Untyped credential-bearing exception (not a ConnectorError/
    SQLAlchemyError) mid-loop must still be caught, scrubbed, and the loop
    must continue."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    exc = ValueError("connection failed: postgresql://u:secretpw@h/d")
    monkeypatch.setattr(
        sync, "get_connector",
        lambda _ds: _MixedFetchShim(source_engine, "bad_table", exc),
    )

    with caplog.at_level("WARNING"):
        result = sync.sync_source(db, ds, ["bad_table", "customers"], app_engine=app_engine)

    # Failed table: scrubbed error recorded, loop did not abort.
    assert "secretpw" not in result["errors"]["bad_table"]
    assert "h" in result["errors"]["bad_table"]
    # Second table: synced normally despite the first table's untyped failure.
    assert result["synced"] == ["customers"]
    assert result["rows"]["customers"] == 2

    warning_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretpw" not in warning_text


def test_run_due_syncs_continues_after_untyped_source_failure(monkeypatch, app_db, caplog):
    """One due source's sync_source raises an untyped (non-ConnectorError/
    SQLAlchemyError) exception; the remaining due source must still be
    synced, and the log must be scrubbed."""
    app_engine, db = app_db
    bad = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="bad-src",
                        source_type="postgres", host="x", port=1, database="d",
                        username="u", enabled=True,
                        options=json.dumps({"sync": {"enabled": True, "interval_minutes": 60,
                                                     "tables": ["t"]}}))
    good = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="good-src",
                         source_type="postgres", host="x", port=1, database="d",
                         username="u", enabled=True,
                         options=json.dumps({"sync": {"enabled": True, "interval_minutes": 60,
                                                      "tables": ["t"]}}))
    db.add(bad)
    db.add(good)
    db.commit()

    def _fake_sync_source(db_, ds, tables, app_engine=None):
        if ds.id == bad.id:
            raise ValueError("connection failed: postgresql://u:secretpw@h/d")
        return {"synced": tables, "rows": {}, "errors": {}}

    monkeypatch.setattr(sync, "sync_source", _fake_sync_source)

    with caplog.at_level("WARNING"):
        done = sync.run_due_syncs(db, app_engine=app_engine)

    assert bad.id not in done
    assert good.id in done  # the remaining due source still synced

    warning_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "secretpw" not in warning_text
    assert "scheduled sync" in warning_text


# --- MD-01: MAX_SYNC_ROWS truncation must surface a truncated flag, not a
# silent, indistinguishable-from-complete success ---

def test_sync_source_surfaces_truncated_flag(monkeypatch, tmp_path, app_db):
    """When a table's row count hits MAX_SYNC_ROWS, sync_source must report
    truncated=True / rows_truncated[table]=True and set
    last_sync_status='ok_truncated' -- not a bare 'ok' indistinguishable from
    a fully-synced table."""
    app_engine, db = app_db
    eng = create_engine(f"sqlite:///{tmp_path/'src_trunc.db'}")
    md = MetaData()
    widgets = Table("widgets", md, Column("id", String, primary_key=True), Column("name", String))
    md.create_all(eng)
    with eng.begin() as c:
        c.execute(insert(widgets), [{"id": str(i), "name": f"w{i}"} for i in range(5)])

    # Shrink both the batch size and the row cap so the guard fires well
    # inside a small, fast test fixture instead of needing 1M+ real rows.
    monkeypatch.setattr(sync, "_BATCH", 2)
    monkeypatch.setattr(sync, "MAX_SYNC_ROWS", 2)

    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return eng
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    result = sync.sync_source(db, ds, ["widgets"], app_engine=app_engine)

    assert result["synced"] == ["widgets"]
    assert result["errors"] == {}
    assert result["rows"]["widgets"] == 2  # stopped at the (patched) cap, not all 5 source rows
    assert result["truncated"] is True
    assert result["rows_truncated"]["widgets"] is True
    assert ds.last_sync_status == "ok_truncated"


def test_sync_source_no_truncated_flag_under_cap(monkeypatch, source_engine, app_db):
    """A normal, complete sync (row count well under MAX_SYNC_ROWS) must NOT
    set the truncated flag -- this is a happy-path regression lock, not a
    truncation test."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return source_engine
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    result = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    assert result["truncated"] is False
    assert result["rows_truncated"] == {}
    assert ds.last_sync_status == "ok"


# --- QB-17/D-16: a connector-signalled BYTE-cap truncation (SyncTable.
# truncated=True -- File/REST's streaming budget cap) must surface through
# the SAME MD-01 indicator as the existing row-cap path ---

def test_sync_source_surfaces_connector_byte_truncated_flag(monkeypatch, app_db):
    """A connector returning SyncTable(truncated=True) -- e.g. File/REST
    hitting MAX_SYNC_BYTES well before MAX_SYNC_ROWS -- must set
    out['truncated']/rows_truncated[table] and last_sync_status=
    'ok_truncated', identical in shape to the row-cap path, even though the
    actual row count is tiny."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _ByteTruncatedShim(dc.DataSourceConnector):
        def make_engine(self):
            raise AssertionError("should not be called")

        def fetch_table(self, name: str):
            return dc.SyncTable(
                columns=[dc.SyncColumn("id", String()), dc.SyncColumn("name", String())],
                rows=iter([{"id": "1", "name": "Acme"}]),
                truncated=True,
            )
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _ByteTruncatedShim())

    result = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    assert result["errors"] == {}
    # Well under MAX_SYNC_ROWS -- proves the BYTE cap (not the row cap) drove
    # this truncation.
    assert result["rows"]["customers"] == 1
    assert result["truncated"] is True
    assert result["rows_truncated"]["customers"] is True
    assert ds.last_sync_status == "ok_truncated"


# --- QB-06: sync_started_at column + dialect-agnostic migration ---

def test_sync_started_at_column_present():
    """A fresh DB (create_all straight from the model) already has the
    sync_started_at column -- no migration needed on a new install."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=eng)
    with eng.connect() as c:
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(qb_data_sources)"))]
    assert "sync_started_at" in cols


def test_migrate_adds_sync_started_at_to_legacy_db(tmp_path, monkeypatch):
    """A legacy qb_data_sources table (built without sync_started_at) gets the
    column added by the migration; a second run is a clean no-op."""
    eng = create_engine(f"sqlite:///{tmp_path/'legacy.db'}")
    md = MetaData()
    Table("qb_data_sources", md,
          Column("id", String, primary_key=True),
          Column("name", String),
          Column("last_sync_status", String))
    md.create_all(eng)

    monkeypatch.setattr(database, "engine", eng)

    with eng.connect() as c:
        cols_before = [r[1] for r in c.execute(text("PRAGMA table_info(qb_data_sources)"))]
    assert "sync_started_at" not in cols_before

    database._migrate_add_qb_sync_started_at_column()

    with eng.connect() as c:
        cols_after = [r[1] for r in c.execute(text("PRAGMA table_info(qb_data_sources)"))]
    assert "sync_started_at" in cols_after

    # Idempotent: re-running against an already-migrated DB does not raise.
    database._migrate_add_qb_sync_started_at_column()
    with eng.connect() as c:
        cols_again = [r[1] for r in c.execute(text("PRAGMA table_info(qb_data_sources)"))]
    assert cols_again.count("sync_started_at") == 1


# --- QB-06: NULL-safe atomic CAS concurrency guard (RED until Task 3) ---

def test_stale_lock_is_reclaimed(app_db):
    """A 'running' lock older than the TTL, or with a NULL sync_started_at
    (pre-migration row), is reclaimable; a fresh 'running' lock is not."""
    app_engine, db = app_db
    now = utcnow_naive()
    stale_ts = now - timedelta(minutes=31)

    ds_stale = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="stale",
                             source_type="postgres", host="x", port=1, database="d",
                             username="u", last_sync_status="running", sync_started_at=stale_ts)
    ds_null = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="nullstart",
                            source_type="postgres", host="x", port=1, database="d",
                            username="u", last_sync_status="running", sync_started_at=None)
    ds_fresh = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="fresh",
                             source_type="postgres", host="x", port=1, database="d",
                             username="u", last_sync_status="running", sync_started_at=now)
    db.add_all([ds_stale, ds_null, ds_fresh])
    db.commit()

    assert sync._try_acquire_sync_lock(db, ds_stale.id) is True
    assert sync._try_acquire_sync_lock(db, ds_null.id) is True
    assert sync._try_acquire_sync_lock(db, ds_fresh.id) is False


# --- CR-01/LW-01: a connector-construction failure must not wedge the lock ---
# (real sync_source path, real SQLite session/DataSource row -- not a mocked
# sync.sync_source). RED against the pre-fix code: get_connector() raised
# unguarded right after the CAS lock acquire, so the exception skipped the
# post-loop status update entirely and left last_sync_status="running" +
# a fresh sync_started_at wedged for the full 30-minute TTL.

def test_connector_construction_failure_releases_lock(monkeypatch, app_db):
    """A real get_connector() failure (e.g. an SSRF-blocked host or an
    unsupported source_type) must (a) still propagate as the real error, and
    (b) leave the lock genuinely free -- a subsequent
    _try_acquire_sync_lock must succeed IMMEDIATELY, with no 30-minute TTL
    wait, and last_sync_status must not be left as "running"."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    def _raise_connector_error(_ds):
        raise dc.ConnectorError("This database host is not allowed: blocked SSRF target")
    monkeypatch.setattr(sync, "get_connector", _raise_connector_error)

    with pytest.raises(dc.ConnectorError):
        sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    # The real error propagated (not swallowed as "already_running" on this
    # very call), and the lock was released rather than left "running".
    db.refresh(ds)
    assert ds.last_sync_status != "running"
    assert ds.last_sync_status == "error"
    assert "blocked SSRF target" in (ds.last_sync_error or "")
    assert ds.sync_started_at is None  # LW-01: TTL clock cleared along with the lock

    # A subsequent lock acquisition succeeds immediately -- no 30-min TTL wait.
    assert sync._try_acquire_sync_lock(db, ds.id) is True


class _SlowFetchShim(dc.DataSourceConnector):
    """Same shape as the plain SQLite shim used elsewhere in this file, but
    fetch_table sleeps briefly before reflecting/streaming. A 2-row SQLite
    sync otherwise completes (acquire -> materialize -> release) in well
    under a millisecond -- fast enough that the "losing" thread can be
    scheduled by the OS *after* the winner has already released the lock,
    which is a legitimate (not a bug) outcome but makes the overlap
    non-deterministic to assert on. The sleep widens the critical section so
    both threads are guaranteed to be inside sync_source's guarded region at
    the same time, making the "exactly one winner" assertion deterministic."""

    def __init__(self, engine):
        self._engine = engine

    def make_engine(self):
        return self._engine

    def fetch_table(self, name: str):
        import time
        time.sleep(0.1)
        return super().fetch_table(name)


def test_concurrent_sync_serializes_via_cas(monkeypatch, tmp_path):
    """Two threads racing sync_source for the SAME DataSource must serialize:
    exactly one wins the CAS lock and syncs, the other returns a clean
    already-running result with zero table writes. The mirror ends up
    internally consistent -- never a half-written interleave."""
    source_engine = create_engine(f"sqlite:///{tmp_path/'cas_src.db'}",
                                  connect_args={"check_same_thread": False})
    md = MetaData()
    customers = Table("customers", md,
                      Column("id", String, primary_key=True), Column("name", String))
    md.create_all(source_engine)
    with source_engine.begin() as c:
        c.execute(insert(customers), [{"id": "c1", "name": "Acme"},
                                      {"id": "c2", "name": "Globex"}])

    app_engine = create_engine(f"sqlite:///{tmp_path/'cas_app.db'}",
                               connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=app_engine)
    from sqlalchemy.orm import sessionmaker
    SessionFactory = sessionmaker(bind=app_engine)

    seed_db = SessionFactory()
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    seed_db.add(ds)
    seed_db.commit()
    ds_id = ds.id
    seed_db.close()

    monkeypatch.setattr(sync, "get_connector", lambda _ds: _SlowFetchShim(source_engine))

    barrier = threading.Barrier(2)
    results = [None, None]

    def _worker(idx):
        db = SessionFactory()
        ds_local = db.query(qm.DataSource).filter(qm.DataSource.id == ds_id).first()
        barrier.wait()
        try:
            results[idx] = sync.sync_source(db, ds_local, ["customers"], app_engine=app_engine)
        finally:
            db.close()

    t1 = threading.Thread(target=_worker, args=(0,))
    t2 = threading.Thread(target=_worker, args=(1,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert results[0] is not None and results[1] is not None
    already_running_flags = [bool(r.get("already_running")) for r in results]
    synced_flags = [bool(r.get("synced")) for r in results]
    assert already_running_flags.count(True) == 1
    assert synced_flags.count(True) == 1

    # Mirror is internally consistent: matches the source, never half-written.
    # Replace mode (QB-05) stages into a uniquely-named table and repoints
    # the catalog pointer to it -- query via that pointer, not the
    # deterministic physical_name() base.
    check_db = SessionFactory()
    ct = check_db.query(qm.CatalogTable).filter(
        qm.CatalogTable.source_id == ds_id, qm.CatalogTable.name == "customers").one()
    with app_engine.connect() as c:
        names = {r[0] for r in c.execute(text(f'SELECT name FROM "{ct.physical_table_name}" ORDER BY name'))}
    check_db.close()
    assert names == {"Acme", "Globex"}


# --- QB-05: crash-safe full-refresh (staging table + atomic pointer swap) ---
# These two tests are RED against the current in-place drop/create
# `materialize_synctable`: test_partial_failure_preserves_prior_mirror proves
# the existing silent-data-loss bug (the mirror is emptied by a mid-fetch
# connector error), and test_replace_mode_stages_and_swaps proves no staging
# table exists yet. Both go GREEN once the staging+swap rework (Tasks 2-3)
# lands.

class _MidFailFetchShim(dc.DataSourceConnector):
    """Connector whose fetch_table streams `fail_after` rows from the real
    reflect-and-stream implementation and then raises dc.ConnectorError --
    reproducing a connector dying partway through a replace-mode fill. Stays
    inside the narrowed `except (ConnectorError, SQLAlchemyError)` (WR-04)."""

    def __init__(self, engine, fail_after=1):
        self._engine = engine
        self._fail_after = fail_after

    def make_engine(self):
        return self._engine

    def fetch_table(self, name: str):
        real = super().fetch_table(name)

        def _rows():
            for i, row in enumerate(real.rows):
                if i >= self._fail_after:
                    break
                yield row
            raise dc.ConnectorError("simulated mid-sync connector failure")

        return dc.SyncTable(columns=real.columns, rows=_rows())


def test_partial_failure_preserves_prior_mirror(monkeypatch, source_engine, app_db):
    """A mid-sync connector failure during a replace-mode refresh must leave
    the PRIOR mirror + its CatalogTable.physical_table_name fully intact and
    queryable (QB-05 [HIGH] / D-01, D-02) -- never an empty/missing table."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return source_engine
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    # First sync succeeds -- capture the mirror's identity + contents.
    result1 = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)
    assert result1["synced"] == ["customers"]
    ct = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).one()
    prior_phys = ct.physical_table_name
    with app_engine.connect() as c:
        prior_names = {r[0] for r in c.execute(text(f'SELECT name FROM "{prior_phys}" ORDER BY name'))}
    assert prior_names == {"Acme", "Globex"}

    # Second sync's connector dies partway through the fill.
    monkeypatch.setattr(sync, "get_connector",
                        lambda _ds: _MidFailFetchShim(source_engine, fail_after=1))
    result2 = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    assert "customers" in result2["errors"]

    # The prior mirror + pointer are UNCHANGED and still answer queries.
    db.refresh(ct)
    assert ct.physical_table_name == prior_phys
    with app_engine.connect() as c:
        names_after = {r[0] for r in c.execute(text(f'SELECT name FROM "{prior_phys}" ORDER BY name'))}
    assert names_after == {"Acme", "Globex"}


def test_replace_mode_stages_and_swaps(monkeypatch, source_engine, app_db):
    """A successful replace-mode refresh materializes into a fresh, uniquely
    named staging table and repoints CatalogTable.physical_table_name to it in
    ONE committed transaction; the previous physical table is dropped
    afterward, leaving exactly one surviving ext_* mirror for this logical
    table and no CatalogTable/CatalogField duplication (QB-05 [HIGH] / D-01,
    D-04)."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return source_engine
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    result1 = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)
    assert result1["rows"]["customers"] == 2
    ct = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).one()
    first_phys = ct.physical_table_name

    # Change the source data entirely, then sync again.
    with source_engine.begin() as c:
        c.execute(text("DELETE FROM customers"))
        c.execute(text(
            "INSERT INTO customers (id, name) VALUES ('c3', 'Foo'), ('c4', 'Bar'), ('c5', 'Baz')"
        ))

    result2 = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)
    assert result2["synced"] == ["customers"]
    assert result2["rows"]["customers"] == 3

    db.refresh(ct)
    second_phys = ct.physical_table_name
    assert second_phys != first_phys
    assert re.match(r"^ext_[A-Za-z0-9_]+$", second_phys)

    with app_engine.connect() as c:
        names = {r[0] for r in c.execute(text(f'SELECT name FROM "{second_phys}" ORDER BY name'))}
    assert names == {"Foo", "Bar", "Baz"}

    # Exactly one physical mirror for this logical table survives -- the old
    # one was dropped after the pointer swap committed.
    phys_base = sync.physical_name(ds.id, "customers")
    inspector = inspect(app_engine)
    surviving = [t for t in inspector.get_table_names()
                 if t == phys_base or t.startswith(phys_base + "__")]
    assert surviving == [second_phys]

    # No catalog-row duplication across syncs.
    assert db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).count() == 1


# --- QB-07: schema-drift-on-re-sync detection (name-only added/removed) ---
# RED until Task 2 lands _diff_catalog_columns + the drift key in the sync
# result.

def test_drift_reported_added_removed_columns(monkeypatch, tmp_path, app_db):
    """A re-sync whose source added/removed a column reports {added, removed}
    in the sync result WITHOUT blocking the sync -- the mirror still updates
    to the live shape (QB-07 / D-13)."""
    app_engine, db = app_db
    eng = create_engine(f"sqlite:///{tmp_path/'src_drift.db'}")
    md = MetaData()
    customers = Table("customers", md,
                      Column("id", String, primary_key=True), Column("name", String))
    md.create_all(eng)
    with eng.begin() as c:
        c.execute(insert(customers), [{"id": "c1", "name": "Acme"}])

    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return eng
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    result1 = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)
    assert result1["synced"] == ["customers"]
    # First-ever sync of this table has no prior CatalogField snapshot to
    # diff against -- nothing to report.
    assert result1.get("drift", {}).get("customers") is None

    # Source schema changes: 'name' dropped, 'email' added.
    with eng.begin() as c:
        c.execute(text("DROP TABLE customers"))
    md2 = MetaData()
    Table("customers", md2, Column("id", String, primary_key=True), Column("email", String))
    md2.create_all(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO customers (id, email) VALUES ('c1', 'acme@example.com')"))

    result2 = sync.sync_source(db, ds, ["customers"], app_engine=app_engine)

    # The sync still SUCCEEDS despite the schema change (non-blocking, D-13).
    assert result2["synced"] == ["customers"]
    assert result2["errors"] == {}
    assert result2["drift"]["customers"] == {"added": ["email"], "removed": ["name"]}

    # The mirror + CatalogField rows now reflect the LIVE {id, email} shape.
    ct = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).one()
    fields = {f.name for f in db.query(qm.CatalogField).filter(qm.CatalogField.table_id == ct.id)}
    assert fields == {"id", "email"}
    with app_engine.connect() as c:
        rows = list(c.execute(text(f'SELECT id, email FROM "{ct.physical_table_name}"')))
    assert rows == [("c1", "acme@example.com")]


# --- QB-18: orphan sweep + SC-5 integration-checker proof (RED until Task 2/3) ---
# `sweep_orphan_ext_tables` / relocated `_SAFE_PHYSICAL_TABLE` do not exist on
# `src.datasource_sync` yet -- both tests below fail on AttributeError until
# Task 2 (sweep) and Task 3 (wiring + SC-5 proof) land.

def test_orphan_sweep_drops_unpointed_ext_tables_only(app_db):
    """The sweep drops ONLY an ext_*-shaped physical table with no CURRENT
    CatalogTable.physical_table_name pointer; a live mirror (pointed-to by a
    CatalogTable) and a non-mirror-shaped table are both left untouched
    (QB-18 / D-15)."""
    app_engine, db = app_db
    md = MetaData()
    Table("ext_orphan_x", md, Column("id", String, primary_key=True))
    Table("ext_live_y", md, Column("id", String, primary_key=True))
    Table("user_notes", md, Column("id", String, primary_key=True))
    md.create_all(app_engine)

    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()
    ct = qm.CatalogTable(id=str(uuid.uuid4()).replace("-", ""), source_id=ds.id, name="live",
                         display_name="live", is_internal=False,
                         physical_table_name="ext_live_y")
    db.add(ct)
    db.commit()

    dropped = sync.sweep_orphan_ext_tables(db, app_engine)

    assert dropped == ["ext_orphan_x"]
    names = set(inspect(app_engine).get_table_names())
    assert "ext_orphan_x" not in names
    assert "ext_live_y" in names
    assert "user_notes" in names


def test_orphan_sweep_never_drops_reserved_in_flight_staging_table(app_db):
    """HG-01: a staging-shaped table that is registered as an in-flight
    reservation (a DIFFERENT, concurrently-syncing source's staging table,
    CREATEd but not yet pointed at by a committed CatalogTable row) must NOT
    be dropped by sweep_orphan_ext_tables, even though it is indistinguishable
    by name/shape from a settled mirror. A genuinely settled, unpointed
    ext_* table (no reservation) is still dropped as before."""
    app_engine, db = app_db
    phys = sync.physical_name(str(uuid.uuid4()).replace("-", ""), "customers")
    staging_name = sync._new_staging_name(phys)

    md = MetaData()
    Table(staging_name, md, Column("id", String, primary_key=True))
    Table("ext_settled_orphan", md, Column("id", String, primary_key=True))
    md.create_all(app_engine)

    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    sync._reserve_staging_table(staging_name)
    try:
        dropped = sync.sweep_orphan_ext_tables(db, app_engine)
    finally:
        sync._release_staging_table(staging_name)

    assert dropped == ["ext_settled_orphan"]
    names = set(inspect(app_engine).get_table_names())
    assert staging_name in names  # in-flight staging table survives
    assert "ext_settled_orphan" not in names  # settled orphan still swept

    # Once released, the same table (if still unpointed) is no longer
    # protected -- confirms the guard is a genuine time-bounded reservation,
    # not a permanent exclusion of that name.
    dropped2 = sync.sweep_orphan_ext_tables(db, app_engine)
    assert dropped2 == [staging_name]


def test_sc5_integration_partial_failure_and_overlap(monkeypatch, tmp_path):
    """SC-5 (ROADMAP integration-checker connection): (a) a mid-sync connector
    failure during a replace-mode refresh leaves the PRIOR mirror + catalog
    pointer fully intact on the manual path; (b) a concurrent manual
    `sync_source` call and a scheduled `run_due_syncs` call for the SAME
    DataSource serialize -- exactly one performs the real connector fetch,
    the other is cleanly skipped -- leaving the mirror internally consistent.
    Both halves exercise real SQLite tables, never mocks/logs."""
    source_engine = create_engine(f"sqlite:///{tmp_path/'sc5_src.db'}",
                                  connect_args={"check_same_thread": False})
    md = MetaData()
    customers = Table("customers", md,
                      Column("id", String, primary_key=True), Column("name", String))
    md.create_all(source_engine)
    with source_engine.begin() as c:
        c.execute(insert(customers), [{"id": "c1", "name": "Acme"},
                                      {"id": "c2", "name": "Globex"}])

    app_engine = create_engine(f"sqlite:///{tmp_path/'sc5_app.db'}",
                               connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=app_engine)
    from sqlalchemy.orm import sessionmaker
    SessionFactory = sessionmaker(bind=app_engine)

    # --- Part (a): mid-sync failure preserves the prior mirror (manual path,
    # reuses plan-03's test_partial_failure_preserves_prior_mirror shape). ---
    db = SessionFactory()
    ds1 = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="sc5-manual",
                        source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds1)
    db.commit()

    class _Shim(dc.DataSourceConnector):
        def make_engine(self):
            return source_engine
    monkeypatch.setattr(sync, "get_connector", lambda _ds: _Shim())

    result1 = sync.sync_source(db, ds1, ["customers"], app_engine=app_engine)
    assert result1["synced"] == ["customers"]
    ct1 = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds1.id).one()
    prior_phys = ct1.physical_table_name
    with app_engine.connect() as c:
        prior_names = {r[0] for r in c.execute(text(f'SELECT name FROM "{prior_phys}" ORDER BY name'))}
    assert prior_names == {"Acme", "Globex"}

    monkeypatch.setattr(sync, "get_connector",
                        lambda _ds: _MidFailFetchShim(source_engine, fail_after=1))
    result2 = sync.sync_source(db, ds1, ["customers"], app_engine=app_engine)
    assert "customers" in result2["errors"]

    db.refresh(ct1)
    assert ct1.physical_table_name == prior_phys
    with app_engine.connect() as c:
        names_after = {r[0] for r in c.execute(text(f'SELECT name FROM "{prior_phys}" ORDER BY name'))}
    assert names_after == {"Acme", "Globex"}
    db.close()

    # --- Part (b): concurrent manual + scheduled overlap serializes (D-12). ---
    ds2 = qm.DataSource(
        id=str(uuid.uuid4()).replace("-", ""), name="sc5-overlap",
        source_type="postgres", host="x", port=1, database="d", username="u",
        enabled=True,
        options=json.dumps({"sync": {"enabled": True, "interval_minutes": 60,
                                     "tables": ["customers"]}}),
    )
    seed_db = SessionFactory()
    seed_db.add(ds2)
    seed_db.commit()
    ds2_id = ds2.id
    seed_db.close()

    call_lock = threading.Lock()
    call_count = [0]

    class _CountingSlowShim(dc.DataSourceConnector):
        """Counts real fetch_table invocations and sleeps briefly to widen
        the critical section -- same rationale as _SlowFetchShim above: a
        fast 2-row sync could otherwise complete before the OS schedules the
        losing thread, making the overlap non-deterministic to assert on."""

        def __init__(self, engine):
            self._engine = engine

        def make_engine(self):
            return self._engine

        def fetch_table(self, name: str):
            with call_lock:
                call_count[0] += 1
            import time
            time.sleep(0.1)
            return super().fetch_table(name)

    monkeypatch.setattr(sync, "get_connector", lambda _ds: _CountingSlowShim(source_engine))

    barrier = threading.Barrier(2)
    results = {}

    def _manual():
        db_m = SessionFactory()
        ds_local = db_m.query(qm.DataSource).filter(qm.DataSource.id == ds2_id).first()
        barrier.wait()
        try:
            results["manual"] = sync.sync_source(db_m, ds_local, ["customers"], app_engine=app_engine)
        finally:
            db_m.close()

    def _scheduled():
        db_s = SessionFactory()
        barrier.wait()
        try:
            results["scheduled"] = sync.run_due_syncs(db_s, app_engine=app_engine)
        finally:
            db_s.close()

    t1 = threading.Thread(target=_manual)
    t2 = threading.Thread(target=_scheduled)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Exactly one caller actually performed the connector fetch -- the CAS
    # loser (whichever path it is) never touches the connector at all, and
    # never raises.
    assert call_count[0] == 1
    assert results.get("manual") is not None
    assert results.get("scheduled") is not None
    assert results["manual"].get("errors") == {}
    manual_won = bool(results["manual"].get("synced"))
    manual_lost_cleanly = bool(results["manual"].get("already_running"))
    assert manual_won != manual_lost_cleanly  # exactly one of these is true

    # The mirror is internally consistent -- matches the source, never a
    # half-written interleave -- regardless of which path won.
    check_db = SessionFactory()
    ct2 = check_db.query(qm.CatalogTable).filter(
        qm.CatalogTable.source_id == ds2_id, qm.CatalogTable.name == "customers").one()
    with app_engine.connect() as c:
        names2 = {r[0] for r in c.execute(text(f'SELECT name FROM "{ct2.physical_table_name}" ORDER BY name'))}
    check_db.close()
    assert names2 == {"Acme", "Globex"}


# --- T-22-18-DROP gap-closure (22-SECURITY.md audit finding): the HG-01
# registry only reserved REPLACE-mode staging tables; an UPSERT-mode table's
# first-ever-sync CREATE window (before any CatalogTable row is committed)
# was unreserved and indistinguishable from a genuine orphan. ---

def test_orphan_sweep_never_drops_inflight_upsert_first_sync_table(monkeypatch, source_engine, app_db):
    """A concurrent, UNRELATED source's sweep_orphan_ext_tables() must never
    drop an upsert-mode table that is mid-fill on its first-ever sync (no
    CatalogTable row committed yet for it), even though the table is
    `_SAFE_PHYSICAL_TABLE`-shaped and absent from the "live" pointer set --
    exactly the same shape a genuine orphan has. A genuinely orphaned,
    unpointed table is still dropped by that same concurrent sweep call.

    Simulates the overlap without threads: the upsert-mode connector's row
    iterator invokes the REAL `sync.sweep_orphan_ext_tables` (the exact call
    a DIFFERENT, concurrently-finishing source's own `sync_source` makes at
    the end of ITS sync) partway through the fetch -- i.e. AFTER
    materialize_synctable's `local.create(checkfirst=True)` has already made
    the upsert-mode physical table visible to `inspect()`, but BEFORE
    `_upsert_catalog` commits a CatalogTable row pointing at it. This is a
    real call into `sweep_orphan_ext_tables` against the real `app_engine`
    and a real second DB session -- not a mock."""
    app_engine, db = app_db
    from sqlalchemy.orm import sessionmaker
    SessionFactory = sessionmaker(bind=app_engine)

    # A genuinely orphaned, unpointed table that must still be swept away.
    md = MetaData()
    Table("ext_settled_orphan", md, Column("id", String, primary_key=True))
    md.create_all(app_engine)

    ds_a = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src-a-upsert",
                        source_type="postgres", host="x", port=1, database="d", username="u",
                        options=json.dumps({"sync": {"mode": "upsert",
                                                      "keys": {"customers": "id"}}}))
    db.add(ds_a)
    db.commit()
    phys_a = sync.physical_name(ds_a.id, "customers")

    class _ConcurrentSweepDuringUpsertFetchShim(dc.DataSourceConnector):
        """Real reflect-and-stream fetch_table, but the row generator runs
        the real sweep_orphan_ext_tables (as an unrelated, concurrently
        syncing DataSource's own sync_source call would, on ITS OWN db
        session) right after the first row -- by which point
        materialize_synctable's local.create(checkfirst=True) has already
        run (it happens before any row is consumed)."""

        def __init__(self, engine):
            self._engine = engine
            self.swept = None

        def make_engine(self):
            return self._engine

        def fetch_table(self, name):
            real = super().fetch_table(name)
            outer = self

            def _rows():
                for i, row in enumerate(real.rows):
                    yield row
                    if i == 0 and outer.swept is None:
                        concurrent_db = SessionFactory()
                        try:
                            outer.swept = sync.sweep_orphan_ext_tables(concurrent_db, app_engine)
                        finally:
                            concurrent_db.close()
            return dc.SyncTable(columns=real.columns, rows=_rows())

    shim = _ConcurrentSweepDuringUpsertFetchShim(source_engine)
    monkeypatch.setattr(sync, "get_connector", lambda _ds: shim)

    result = sync.sync_source(db, ds_a, ["customers"], app_engine=app_engine)

    # The sync itself must succeed cleanly -- the in-flight table was never
    # yanked out from under it.
    assert result["synced"] == ["customers"]
    assert result["errors"] == {}
    assert result["rows"]["customers"] == 2

    # The concurrent sweep actually ran and did its job: it swept the
    # genuinely orphaned table but NOT the in-flight upsert table.
    assert shim.swept is not None
    assert "ext_settled_orphan" in shim.swept
    assert phys_a not in shim.swept

    names_after_concurrent_sweep = set(inspect(app_engine).get_table_names())
    assert phys_a in names_after_concurrent_sweep  # survived the concurrent sweep
    assert "ext_settled_orphan" not in names_after_concurrent_sweep

    # The sync completed and the catalog row now points at the surviving
    # physical table, with the correct final data (mid-fill drop would have
    # emptied/removed it instead).
    ct = db.query(qm.CatalogTable).filter(
        qm.CatalogTable.source_id == ds_a.id, qm.CatalogTable.name == "customers").one()
    assert ct.physical_table_name == phys_a
    with app_engine.connect() as c:
        final_names = {r[0] for r in c.execute(text(f'SELECT name FROM "{phys_a}" ORDER BY name'))}
    assert final_names == {"Acme", "Globex"}


# ---------------------------------------------------------------------------
# QB-10: stable type inference -- resolve_effective_types() precedence
# (override > prior CatalogField.data_type > freshly inferred) + the
# no-split-brain guarantee that the mirror DDL and the catalog write always
# agree on a column's type after a re-sync.
# ---------------------------------------------------------------------------

def test_type_new_column_infers_fresh():
    """No prior CatalogField row and no override -- a brand-new column keeps
    whatever type the connector freshly inferred."""
    st = dc.SyncTable(columns=[dc.SyncColumn("amount", Integer())], rows=iter([]))

    sync.resolve_effective_types([], st, {})

    assert isinstance(st.columns[0].type_, Integer)


def test_type_prior_wins():
    """A prior CatalogField.data_type wins over a churned fresh inference --
    the exact QB-10 stability guarantee (int one run, str the next must not
    flip the SQL type)."""
    prior_field = qm.CatalogField(id="f1", table_id="t1", name="amount", data_type="INTEGER")
    st = dc.SyncTable(columns=[dc.SyncColumn("amount", String())], rows=iter([]))

    sync.resolve_effective_types([prior_field], st, {})

    assert isinstance(st.columns[0].type_, Integer)


def test_type_override_wins():
    """An explicit type_overrides entry wins over BOTH the prior CatalogField
    type and the freshly inferred type -- the user's escape hatch (D-13)."""
    prior_field = qm.CatalogField(id="f1", table_id="t1", name="amount", data_type="INTEGER")
    st = dc.SyncTable(columns=[dc.SyncColumn("amount", String())], rows=iter([]))

    sync.resolve_effective_types([prior_field], st, {"amount": "Float"})

    assert isinstance(st.columns[0].type_, Float)


def test_type_unknown_override_ignored(caplog):
    """An override name absent from the static allowlist is ignored (falls
    through to prior/inferred) and logged -- never raises (D-12)."""
    prior_field = qm.CatalogField(id="f1", table_id="t1", name="amount", data_type="INTEGER")
    st = dc.SyncTable(columns=[dc.SyncColumn("amount", String())], rows=iter([]))

    with caplog.at_level(logging.WARNING):
        sync.resolve_effective_types([prior_field], st, {"amount": "Blob"})

    assert isinstance(st.columns[0].type_, Integer)  # fell through to prior
    assert any("Blob" in r.message for r in caplog.records)


def test_type_resolution_is_static_allowlist_only():
    """Resolution must be a static dict-literal lookup -- never
    getattr(sqlalchemy.types, name) on user-controlled input (T-23-07)."""
    assert "Blob" not in sync._TYPE_BY_NAME
    assert set(sync._TYPE_BY_NAME.values()) == {
        __import__("sqlalchemy").types.Integer,
        __import__("sqlalchemy").types.Float,
        __import__("sqlalchemy").types.Boolean,
        __import__("sqlalchemy").types.String,
        __import__("sqlalchemy").types.DateTime,
    }


class _ChurningTypeConnector(dc.DataSourceConnector):
    """A fake connector whose 'amount' column type churns between syncs --
    Integer on the first fetch, String on every subsequent one -- simulating
    the exact QB-10 bug (a column that looks int one run and str the next)."""

    def __init__(self):
        self.calls = 0

    def make_engine(self):
        raise AssertionError("should not be called")

    def fetch_table(self, name: str):
        self.calls += 1
        churned_type = Integer() if self.calls == 1 else String()
        value = 42 if self.calls == 1 else "42"
        return dc.SyncTable(
            columns=[dc.SyncColumn("id", String()), dc.SyncColumn("amount", churned_type)],
            rows=iter([{"id": "1", "amount": value}]),
        )


def test_type_stability_no_split_brain_across_resync(monkeypatch, app_db):
    """Across two syncs where the source's inferred type churns (int then
    str), the re-synced mirror column type and the persisted
    CatalogField.data_type must agree with EACH OTHER and with the PRIOR
    run's type -- no split-brain (D-11)."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u")
    db.add(ds)
    db.commit()

    connector = _ChurningTypeConnector()
    monkeypatch.setattr(sync, "get_connector", lambda _ds: connector)

    sync.sync_source(db, ds, ["items"], app_engine=app_engine)
    sync.sync_source(db, ds, ["items"], app_engine=app_engine)

    assert connector.calls == 2  # confirms the second fetch really did churn

    ct = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).one()
    field = db.query(qm.CatalogField).filter(
        qm.CatalogField.table_id == ct.id, qm.CatalogField.name == "amount").one()

    mirror_cols = {c["name"]: c["type"] for c in inspect(app_engine).get_columns(ct.physical_table_name)}

    # Mirror and catalog agree, and both preserve the FIRST (prior) run's type.
    assert field.data_type == "INTEGER"
    assert "INT" in str(mirror_cols["amount"]).upper()


def test_type_override_applies_on_resync(monkeypatch, app_db):
    """A type_overrides entry in ds.options changes the resolved type
    consistently on the mirror AND the catalog, regardless of prior/fresh."""
    app_engine, db = app_db
    ds = qm.DataSource(id=str(uuid.uuid4()).replace("-", ""), name="src",
                       source_type="postgres", host="x", port=1, database="d", username="u",
                       options=json.dumps({"type_overrides": {"items": {"amount": "Float"}}}))
    db.add(ds)
    db.commit()

    connector = _ChurningTypeConnector()
    monkeypatch.setattr(sync, "get_connector", lambda _ds: connector)

    sync.sync_source(db, ds, ["items"], app_engine=app_engine)
    sync.sync_source(db, ds, ["items"], app_engine=app_engine)

    ct = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).one()
    field = db.query(qm.CatalogField).filter(
        qm.CatalogField.table_id == ct.id, qm.CatalogField.name == "amount").one()
    mirror_cols = {c["name"]: c["type"] for c in inspect(app_engine).get_columns(ct.physical_table_name)}

    assert field.data_type == "FLOAT"
    assert "FLOAT" in str(mirror_cols["amount"]).upper()


def test_upsert_catalog_receives_existing_fields_as_parameter():
    """_upsert_catalog no longer queries CatalogField for the prior snapshot
    itself -- it must accept `existing_fields` as a parameter (Pitfall 6)."""
    import inspect as _pyinspect
    params = list(_pyinspect.signature(sync._upsert_catalog).parameters)
    assert "existing_fields" in params


# --- WR-04 (LOW): materialize_synctable must explicitly .close() the row ---
# generator on every exit path (early MAX_SYNC_ROWS break, exception, or
# normal exhaustion) rather than relying on GC/refcounting -----------------


class _ClosingRows:
    """A generator-shaped test double (not a plain list) that records
    whether/how many times `.close()` was called -- mirrors what a real
    REST/CSV/Excel/SQL connector's generator-based SyncTable.rows exposes
    (every Python generator has a native `.close()`)."""
    def __init__(self, items):
        self._it = iter(items)
        self.closed = 0

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        self.closed += 1


def test_materialize_replace_mode_closes_rows_on_early_truncation(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_BATCH", 1)
    monkeypatch.setattr(sync, "MAX_SYNC_ROWS", 2)
    eng = create_engine(f"sqlite:///{tmp_path/'wr04_replace.db'}")
    rows = _ClosingRows([{"id": str(i), "name": f"w{i}"} for i in range(5)])
    st = dc.SyncTable(
        columns=[dc.SyncColumn("id", String()), dc.SyncColumn("name", String())],
        rows=rows)
    total, staging_name = sync.materialize_synctable(eng, st, "ext_x1_widgets", mode="replace")
    assert total == 2  # stopped at the (patched) row cap -- proves early exit actually happened
    assert rows.closed == 1


def test_materialize_upsert_mode_closes_rows_on_early_truncation(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_BATCH", 1)
    monkeypatch.setattr(sync, "MAX_SYNC_ROWS", 2)
    eng = create_engine(f"sqlite:///{tmp_path/'wr04_upsert.db'}")
    rows = _ClosingRows([{"id": str(i), "name": f"w{i}"} for i in range(5)])
    st = dc.SyncTable(
        columns=[dc.SyncColumn("id", String()), dc.SyncColumn("name", String())],
        rows=rows)
    total = sync.materialize_synctable(eng, st, "ext_x1_widgets_up", mode="upsert", key="id")
    assert total == 2
    assert rows.closed == 1


def test_materialize_closes_rows_on_full_exhaustion_too(tmp_path):
    """Not just the early-break path -- .close() is called even on normal,
    complete exhaustion (a no-op on an already-finished generator, but the
    finally block runs unconditionally)."""
    eng = create_engine(f"sqlite:///{tmp_path/'wr04_full.db'}")
    rows = _ClosingRows([{"id": str(i), "name": f"w{i}"} for i in range(3)])
    st = dc.SyncTable(
        columns=[dc.SyncColumn("id", String()), dc.SyncColumn("name", String())],
        rows=rows)
    total, staging_name = sync.materialize_synctable(eng, st, "ext_x1_widgets2", mode="replace")
    assert total == 3
    assert rows.closed == 1


def test_materialize_replace_mode_plain_list_rows_still_works(tmp_path):
    """A plain list (no .close()) must not raise -- getattr's default no-op
    keeps back-compat for direct callers that hand materialize_synctable an
    already-fully-read list (D-01's own documented back-compat note)."""
    eng = create_engine(f"sqlite:///{tmp_path/'wr04_plainlist.db'}")
    st = dc.SyncTable(
        columns=[dc.SyncColumn("id", String()), dc.SyncColumn("name", String())],
        rows=[{"id": "1", "name": "a"}, {"id": "2", "name": "b"}])
    total, staging_name = sync.materialize_synctable(eng, st, "ext_x1_widgets3", mode="replace")
    assert total == 2
