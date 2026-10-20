"""SC-5 integration proof: a pathological/hung query is bounded by the pool
timeout, a page-ignoring REST endpoint terminates without duplicate
accumulation, and a large source streams to the mirror within the row+byte
budget and is flagged truncated -- proven on REAL paths (live-attribute
spies / real fake sources), never a string-path patch or a smoke-only
presence check (T-23-17, mirrors tests/test_verify_connector_security_
integration.py's assembled/spy idiom).

Three guarantees proven here:
  (a) PATHOLOGICAL QUERY BOUNDED -- `dc._UrlConnector.make_engine()` (the
      exact QB-08 seam `get_connector()` returns) is proven to genuinely
      thread `pool_timeout` into a real SQLAlchemy `QueuePool` (a real
      SQLite engine forced onto it stands in for the unavailable live
      psycopg/PyMySQL driver, per RESEARCH Environment Availability): a
      held-open connection makes a second checkout raise
      `sqlalchemy.exc.TimeoutError` within the bound. A live
      `MagicMock(wraps=dc.create_engine)` spy additionally proves the real
      `get_connector()` genuinely invokes `create_engine` with the
      per-statement/connect timeout kwargs for BOTH a postgres and a mysql
      source (psycopg/PyMySQL themselves are absent on-machine, so the real
      call fails at DBAPI-import time -- irrelevant to this kwarg-plumbing
      proof, which only inspects the spy's genuinely-recorded invocation).
  (b) PAGE-IGNORING ENDPOINT TERMINATES -- a live fake REST client that
      always answers the SAME non-empty page is patched onto the module
      attribute `rest._client` and driven through the REAL end-to-end path
      `sync_source -> get_connector -> RestConnector.fetch_table ->
      materialize_synctable -> CatalogTable`. The synced/mirrored rows equal
      ONE page's worth (the QB-15 dedup guard fired), pagination did not
      loop max_pages, and the termination warning was logged.
  (c) LARGE SOURCE STREAMS WITHIN BUDGET -- a real, on-disk CSV file (CSV is
      used, not Excel, per RESEARCH's Environment Availability note: openpyxl
      is not installed on-machine) far larger than a monkeypatched-small
      MAX_SYNC_BYTES is driven through the REAL `FileConnector.fetch_table`
      generator (a live `MagicMock(wraps=pandas.read_csv)` spy proves the
      chunked `chunksize=` streaming call actually fires, and `st.rows` has
      no `__len__` -- never a materialized whole-file list) AND through the
      full `sync_source` path, asserting `out["truncated"]`/
      `rows_truncated[table]` and `ds.last_sync_status == "ok_truncated"`
      (MD-01 reuse).
"""
import json
import logging
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

import core.constants as constants
import core.querybuilder_models as qm
import src.datasource_connectors as dc
import src.datasource_files as files
import src.datasource_rest as rest
import src.datasource_sync as sync
from core.database import Base
from core.querybuilder_models import DataSource


@pytest.fixture(autouse=True)
def _mock_dns(monkeypatch):
    """Deterministic DNS resolution for both the postgres/mysql
    `db.example.com` fixture host (proof a) and the REST `x.test` fixture
    host (proof b) -- ssrf_guard's resolve_and_pin_host must resolve to a
    real (non-blocked) address for these connector-construction paths to
    proceed past the guard and reach the QB-08/QB-15 behavior under test."""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                 ("203.0.113.9", 0))])


@pytest.fixture
def app_db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    Base.metadata.create_all(bind=eng)
    return eng, sessionmaker(bind=eng)()


# --- (a) PATHOLOGICAL QUERY BOUNDED -----------------------------------------

def test_postgres_and_mysql_timeout_kwargs_reach_real_create_engine_via_live_spy():
    """Genuine-invocation proof: MagicMock(wraps=dc.create_engine) forwards
    to the REAL SQLAlchemy create_engine, proving get_connector()->
    make_engine() actually calls it with QB-08's per-dialect timeout kwargs.
    psycopg/PyMySQL aren't installed on-machine (RESEARCH Environment
    Availability), so the real create_engine call may fail at DBAPI-import
    time for either dialect on this machine -- expected and irrelevant to
    this proof, which only inspects the spy's genuinely-recorded call args
    (never a string-path stub of create_engine that skips the real call)."""
    spy = MagicMock(wraps=dc.create_engine)
    real_create_engine = dc.create_engine
    try:
        dc.create_engine = spy

        pg = DataSource(id="pgX", name="pg", source_type="postgres",
                         host="db.example.com", port=5432, database="mydb",
                         username="u", password="p")
        try:
            dc.get_connector(pg).make_engine()
        except Exception:
            pass  # psycopg absent on-machine; irrelevant to this kwarg proof
        assert spy.called
        pg_kwargs = spy.call_args.kwargs
        assert pg_kwargs["connect_args"]["connect_timeout"] == dc.DEFAULT_CONNECT_TIMEOUT_S
        assert pg_kwargs["connect_args"]["options"] == (
            f"-c statement_timeout={dc.DEFAULT_STATEMENT_TIMEOUT_MS}")
        assert pg_kwargs["connect_args"]["hostaddr"] == "203.0.113.9"
        assert pg_kwargs["pool_timeout"] == dc.DEFAULT_POOL_TIMEOUT_S

        spy.reset_mock()
        my = DataSource(id="myX", name="mysql", source_type="mysql",
                        host="db.example.com", port=3306, database="mydb",
                        username="u", password="p")
        try:
            dc.get_connector(my).make_engine()
        except Exception:
            pass  # PyMySQL absent on-machine; irrelevant to this kwarg proof
        assert spy.called
        my_kwargs = spy.call_args.kwargs
        assert my_kwargs["connect_args"]["connect_timeout"] == dc.DEFAULT_CONNECT_TIMEOUT_S
        assert my_kwargs["connect_args"]["read_timeout"] == (
            dc.DEFAULT_STATEMENT_TIMEOUT_MS / 1000)
        assert my_kwargs["pool_timeout"] == dc.DEFAULT_POOL_TIMEOUT_S
    finally:
        dc.create_engine = real_create_engine


def test_pool_timeout_bounds_a_held_open_checkout_via_real_url_connector(tmp_path, monkeypatch):
    """Real path: `dc._UrlConnector.make_engine()` -- the exact object
    `get_connector()` returns for postgres/mysql -- is proven to enforce its
    `pool_timeout` kwarg via a real SQLAlchemy `QueuePool`. A real SQLite
    engine is forced onto `QueuePool` as the dialect-agnostic stand-in for
    the unavailable live psycopg/PyMySQL driver (RESEARCH Environment
    Availability); `connect_args` is dropped only because sqlite3.connect()
    rejects postgres-only keys (hostaddr/options/connect_timeout) -- the
    pool-bound half is dialect-agnostic by design. A background thread holds
    the one pool slot open; a second checkout through this SAME connector
    object must raise `sqlalchemy.exc.TimeoutError` within the bound."""
    path = tmp_path / "pool.db"
    real_create_engine = create_engine

    def _force_queuepool(url, **kw):
        return real_create_engine(
            url, poolclass=QueuePool, pool_size=1, max_overflow=0,
            pool_timeout=kw["pool_timeout"],
        )
    monkeypatch.setattr(dc, "create_engine", _force_queuepool)

    connector = dc._UrlConnector(f"sqlite:///{path}", connect_args={}, pool_timeout=0.2)
    engine = connector.make_engine()  # the REAL _UrlConnector seam

    held = threading.Event()
    release = threading.Event()

    def _hold_connection():
        conn = engine.connect()
        held.set()
        release.wait(timeout=5)
        conn.close()

    holder = threading.Thread(target=_hold_connection, daemon=True)
    holder.start()
    try:
        assert held.wait(timeout=5), "background thread never acquired the connection"
        start = time.monotonic()
        with pytest.raises(SATimeoutError):
            engine.connect()
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"pool checkout took {elapsed}s — expected ~0.2s bound"
    finally:
        release.set()
        holder.join(timeout=5)
        engine.dispose()


# --- (b) PAGE-IGNORING ENDPOINT TERMINATES ----------------------------------

class _FakeRestResp:
    """A live fake httpx.Response-shaped object -- carries real bytes via
    `.content` so `_response_byte_len` reads actual wire bytes, not the
    json-reencode fallback meant only for bare test doubles."""

    def __init__(self, payload):
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _PageIgnoringRestClient:
    """Live fake REST client always answering the SAME non-empty page --
    simulates an endpoint that ignores the `page` param entirely."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        self.calls.append(dict(params or {}))
        return _FakeRestResp([{"id": 1}, {"id": 2}])


def test_page_ignoring_endpoint_terminates_via_dedup_on_real_sync_path(app_db, monkeypatch, caplog):
    """SC-5 (b): drive `sync_source -> get_connector -> RestConnector.
    fetch_table -> materialize_synctable -> CatalogTable` end to end -- the
    real path the plan's key_links cite -- with a page-ignoring live fake
    client patched onto the LIVE module attribute `rest._client` (never a
    string-path stub of sync_source/fetch_table themselves). Asserts the
    mirrored rows equal ONE page's worth (dedup fired), pagination did not
    loop max_pages, and the termination warning was logged."""
    app_engine, db = app_db
    client = _PageIgnoringRestClient()
    monkeypatch.setattr(rest, "_client", lambda ds: client)  # LIVE attribute spy point

    ds = qm.DataSource(
        id="rest-sc5", name="api", source_type="rest",
        options=json.dumps({
            "base_url": "https://x.test",
            "endpoints": {"users": {
                "path": "/users",
                "pagination": {"type": "page", "param": "page", "max_pages": 50},
            }},
        }))
    db.add(ds)
    db.commit()

    with caplog.at_level(logging.WARNING):
        result = sync.sync_source(db, ds, ["users"], app_engine=app_engine)

    assert result["errors"] == {}
    assert result["synced"] == ["users"]
    # ONE page's worth (2 rows) -- never 50 x 2 -- proves the dedup guard fired.
    assert result["rows"]["users"] == 2
    assert len(client.calls) == 2  # page 1 fetched+yielded; page 2's repeat detected, then stop
    assert any("terminating pagination" in r.message for r in caplog.records)

    # Real end-to-end: the materialized mirror + catalog pointer actually
    # exist and hold exactly the deduped row count -- not a unit-level stub.
    ct = db.query(qm.CatalogTable).filter(qm.CatalogTable.source_id == ds.id).one()
    with app_engine.connect() as conn:
        mirrored = list(conn.execute(text(f'SELECT id FROM "{ct.physical_table_name}"')))
    assert len(mirrored) == 2


# --- (c) LARGE SOURCE STREAMS WITHIN BUDGET ---------------------------------

def test_large_csv_source_streams_within_byte_budget_via_real_sync_path(app_db, tmp_path, monkeypatch):
    """SC-5 (c): drive the REAL `FileConnector.fetch_table` generator
    directly (a live `pandas.read_csv` spy proves it is consumed via
    `pd.read_csv(chunksize=...)`, never a whole-file
    `to_dict(orient="records")` list) AND the full `sync_source ->
    materialize_synctable -> CatalogTable` path (a large CSV, well over a
    monkeypatched-small MAX_SYNC_BYTES, must set `out["truncated"]` and
    `ds.last_sync_status == "ok_truncated"`). CSV is used (not Excel) per
    RESEARCH's Environment Availability note -- openpyxl is not installed
    on-machine."""
    app_engine, db = app_db
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(files, "DATA_DIR", str(tmp_path), raising=False)
    upload_dir = tmp_path / "qb_sources" / "csvsrc"
    upload_dir.mkdir(parents=True)
    p = upload_dir / "big.csv"
    total_rows = 5000
    p.write_text("id,name\n" + "".join(f"{i},row{i}\n" for i in range(total_rows)), encoding="utf-8")

    monkeypatch.setattr(files, "MAX_SYNC_BYTES", 20_000)  # force truncation well before EOF
    monkeypatch.setattr(files, "CSV_CHUNK_ROWS", 200)

    pd = pytest.importorskip("pandas")
    read_csv_spy = MagicMock(wraps=pd.read_csv)
    monkeypatch.setattr(files, "_pandas", lambda: SimpleNamespace(read_csv=read_csv_spy))

    ds = qm.DataSource(id="csv-sc5", name="bigcsv", source_type="csv",
                       options=json.dumps({"file_path": str(p), "kind": "csv"}))

    # (1) Real connector, driven directly: proves the generator shape + the
    # genuine pd.read_csv(chunksize=...) streaming invocation.
    conn = files.FileConnector(ds)
    st = conn.fetch_table("big")
    assert not hasattr(st.rows, "__len__")  # a lazy generator, never a materialized list
    rows = list(st.rows)
    assert st.truncated is True
    assert 0 < len(rows) < total_rows  # streamed partially, never the whole file
    assert any(c.kwargs.get("chunksize") for c in read_csv_spy.call_args_list), (
        "fetch_table must stream via pd.read_csv(chunksize=...), never a whole-file read")

    # (2) Real end-to-end: sync_source -> materialize_synctable -> CatalogTable
    # surfaces the SAME byte-cap truncation through the MD-01 indicator.
    db.add(ds)
    db.commit()
    result = sync.sync_source(db, ds, ["big"], app_engine=app_engine)

    assert result["errors"] == {}
    assert result["truncated"] is True
    assert result["rows_truncated"]["big"] is True
    assert 0 < result["rows"]["big"] < total_rows
    assert ds.last_sync_status == "ok_truncated"
