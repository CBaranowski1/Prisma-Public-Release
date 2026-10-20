"""Data-source routes: privilege gate + create stores encrypted creds."""
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import routes.datasource_routes as mod
import src.datasource_connectors as dc
import src.datasource_sync as sync_mod
from core.querybuilder_models import CatalogField, CatalogTable, DataSource
from src.secret_scrub import scrub_dsn


def _req(user="alice", *, priv_ok):
    privs = {"can_manage_data_sources": priv_ok}
    auth_mgr = SimpleNamespace(get_privileges=lambda u: privs, is_configured=True,
                               is_admin=lambda u: False)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.9"),
    )


def _req_missing_key(user="bob"):
    """A privileges dict that IS present but does not contain
    'can_manage_data_sources' -- the genuine fail-open branch (schema
    drift / legacy auth.json), never an explicit False. Non-loopback host
    mirrors _req's so the loopback bypass never fires."""
    auth_mgr = SimpleNamespace(get_privileges=lambda u: {}, is_configured=True,
                               is_admin=lambda u: False)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.9"),
    )


def _req_lookup_raises(user="bob"):
    """get_privileges() raises -- the second genuine fail-open branch."""
    def _raise(u):
        raise RuntimeError("privilege lookup boom")
    auth_mgr = SimpleNamespace(get_privileges=_raise, is_configured=True,
                               is_admin=lambda u: False)
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth_mgr)),
        client=SimpleNamespace(host="10.0.0.9"),
    )


def _handler(method, fragment):
    for r in mod.setup_datasource_routes().routes:
        if fragment in getattr(r, "path", "") and method in (getattr(r, "methods", None) or set()):
            return r.endpoint
    raise KeyError(fragment)


@contextlib.contextmanager
def _db_ctx(s):
    yield s


def test_list_requires_privilege():
    list_ds = _handler("GET", "/datasources")
    with pytest.raises(HTTPException) as exc:
        list_ds(request=_req("bob", priv_ok=False))
    assert exc.value.status_code == 403


def test_create_persists(monkeypatch):
    captured = {}

    class _DS:
        def __init__(self, **kw):
            captured.update(kw); self.__dict__.update(kw)
        def to_dict(self):
            return {"id": captured.get("id"), "name": captured.get("name"),
                    "source_type": captured.get("source_type")}

    session = MagicMock()
    monkeypatch.setattr(mod, "DataSource", _DS)
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))
    create = _handler("POST", "/datasources")
    body = mod.DataSourceCreate(name="Prod", source_type="postgres", host="h",
                                port=5432, database="d", username="u", password="p")
    resp = create(request=_req("alice", priv_ok=True), body=body)
    assert resp["source_type"] == "postgres"
    assert captured["password"] == "p"   # model's EncryptedText encrypts at write
    assert captured["owner"] == "alice"


# --- 13-06 Task 1: assembled-app /test + /sync credential-scrub regression
# (D-08 end-to-end). No functional change here -- proves the 400 body is
# already clean because scrubbing happened upstream (13-03/13-05).

class _FailingConnector(dc.DataSourceConnector):
    """A REAL DataSourceConnector whose make_engine() always raises a
    DSN-shaped OperationalError. test_connection/list_tables/reflect_table/
    fetch_table are all inherited unmodified from the base class, so driving
    /test through this fake genuinely exercises the D-08 sink (a) scrub
    logic -- not a hand-faked already-clean string."""

    def make_engine(self):
        raise OperationalError(
            "SELECT 1", {}, Exception(
                "could not connect to server: "
                "postgresql://dbuser:secretpw@dbhost.example.com:5432/proddb"))


def _sync_get_connector_raises(ds):
    """Simulates get_connector() itself failing before the per-table sync
    loop starts (mirrors the real SsrfBlocked-to-ConnectorError early-raise
    shape in src.datasource_connectors.get_connector -- sync_source's own
    `connector = get_connector(ds)` line is NOT inside its per-table
    try/except, so a ConnectorError raised here propagates straight to the
    /sync route's `except ConnectorError` -> HTTPException(400, ...)).
    Built via a genuine scrub_dsn() call (not a pre-scrubbed literal), so the
    /sync route's 400 body is proven clean by real invocation."""
    raise dc.ConnectorError(scrub_dsn(
        "could not connect to server: "
        "postgresql://dbuser:secretpw@dbhost.example.com:5432/proddb"))


@pytest.fixture
def _scrub_client(monkeypatch):
    ds = DataSource(id="ds1", name="Prod DB", source_type="postgres",
                    host="dbhost.example.com", port=5432, database="proddb",
                    username="dbuser", password="secretpw")
    session = MagicMock()
    # Phase 22 (QB-06): sync_source now acquires a CAS lock via
    # db.execute(update(...)).rowcount == 1 before doing any work. Let the
    # mocked lock acquire so this test still exercises the connector-failure
    # scrub path (not a spurious already_running short-circuit).
    session.execute.return_value.rowcount = 1
    session.query.return_value.filter.return_value.first.return_value = ds
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))
    monkeypatch.setattr(mod, "require_privilege", lambda request, key, **kw: "alice")
    monkeypatch.setattr(mod, "get_connector", lambda ds: _FailingConnector())
    monkeypatch.setattr(sync_mod, "get_connector", _sync_get_connector_raises)
    app = FastAPI()
    app.include_router(mod.setup_datasource_routes())
    with TestClient(app) as c:
        yield c


def test_route_test_connection_scrub_regression(_scrub_client):
    """POST /test on a credentialed source that fails returns a 400 whose
    body excludes the plaintext password while preserving a non-secret DSN
    token (host), proving scrub -- not blanket redaction (D-08 sink a)."""
    resp = _scrub_client.post("/api/datasources/ds1/test")
    assert resp.status_code == 400, resp.text
    assert "secretpw" not in resp.text
    assert "dbhost.example.com" in resp.text


def test_route_sync_scrub_regression(_scrub_client):
    """POST /sync on a source whose connector construction fails returns a
    400 whose body excludes the plaintext password while preserving a
    non-secret DSN token (D-08, mirrored for the /sync route)."""
    resp = _scrub_client.post(
        "/api/datasources/ds1/sync", json={"tables": ["customers"]})
    assert resp.status_code == 400, resp.text
    assert "secretpw" not in resp.text
    assert "dbhost.example.com" in resp.text


# --- 14-02 Task 1: delete_source D-03 (malformed physical_table_name skips
# the DROP) + D-11 (a DROP failure never blocks DataSource/catalog row
# cleanup) coverage. The file had ZERO delete_source coverage before this.

def _delete_session(cats, ds):
    """MagicMock db session wired for delete_source's query shape:
    db.query(DataSource)...first() -> ds, db.query(CatalogTable)...all() ->
    cats, db.query(CatalogField)...delete() is a harmless no-op."""
    session = MagicMock()

    def _query(model):
        q = MagicMock()
        if model is DataSource:
            q.filter.return_value.first.return_value = ds
        elif model is CatalogTable:
            q.filter.return_value.all.return_value = cats
        elif model is CatalogField:
            q.filter.return_value.delete.return_value = None
        return q

    session.query.side_effect = _query
    return session


def _fake_engine(*, execute_raises=None):
    """A fake `engine` whose `.begin()` context manager's `conn.execute()`
    optionally raises -- lets us drive delete_source's DROP path without a
    real database, and count how many times begin()/execute() actually ran."""
    conn = MagicMock()
    if execute_raises is not None:
        conn.execute.side_effect = execute_raises
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    engine = MagicMock()
    engine.begin.return_value = ctx
    return engine, conn


def test_delete_source_malformed_physical_table_name_skips_drop(monkeypatch, caplog):
    """D-03: a physical_table_name that does NOT match ^ext_[A-Za-z0-9_]+$
    (e.g. tampered/mismatched shape) must never reach the DROP TABLE text()
    interpolation -- skip + log a warning -- and the row must still be
    deleted (best-effort cleanup, D-11)."""
    ds = DataSource(id="ds1", name="Prod", source_type="postgres")
    ct = SimpleNamespace(id="ct1", physical_table_name='ext_a"; DROP TABLE users; --')
    session = _delete_session([ct], ds)
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))
    engine, conn = _fake_engine()
    monkeypatch.setattr(mod, "engine", engine)

    delete_source = _handler("DELETE", "/datasources")
    with caplog.at_level(logging.WARNING):
        resp = delete_source(request=_req("alice", priv_ok=True), ds_id="ds1")

    assert resp == {"status": "deleted"}
    assert engine.begin.call_count == 0, "malformed physical_table_name must skip the DROP entirely"
    assert conn.execute.call_count == 0
    assert any("physical_table_name" in r.message for r in caplog.records)
    session.delete.assert_any_call(ds)
    session.delete.assert_any_call(ct)


def test_delete_source_drop_failure_still_deletes_row(monkeypatch, caplog):
    """D-11: a well-formed physical_table_name whose DROP raises a
    SQLAlchemyError must log a warning naming the orphaned table, must NOT
    re-raise, and the DataSource (+ catalog) row must still be deleted --
    best-effort cleanup is preserved even when the DROP itself fails."""
    ds = DataSource(id="ds1", name="Prod", source_type="postgres")
    ct = SimpleNamespace(id="ct1", physical_table_name="ext_abc123")
    session = _delete_session([ct], ds)
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))
    engine, conn = _fake_engine(
        execute_raises=OperationalError("DROP TABLE", {}, Exception("boom")))
    monkeypatch.setattr(mod, "engine", engine)

    delete_source = _handler("DELETE", "/datasources")
    with caplog.at_level(logging.WARNING):
        resp = delete_source(request=_req("alice", priv_ok=True), ds_id="ds1")

    assert resp == {"status": "deleted"}
    assert engine.begin.call_count == 1, "a well-formed name must still attempt the DROP"
    assert any("ext_abc123" in r.message for r in caplog.records)
    session.delete.assert_any_call(ds)
    session.delete.assert_any_call(ct)


# --- 14-05 gap closure (WR-02 / SEC-04): management-endpoint fail-closed
# matrix. A test that returns identically with or without fail_closed=True
# is exercising the wrong branch (explicit-False) -- these target the
# genuine fail-open branches: privileges dict present but missing the key,
# and a raising privilege lookup. Both are RED until Task 2 wires
# fail_closed=True at the call sites.


def test_delete_source_denies_on_missing_key_fail_closed(monkeypatch):
    """Missing-key fail-open branch (schema drift / legacy auth.json): a
    privileges dict present but without 'can_manage_data_sources' must DENY
    (403) once delete_source opts into fail_closed=True. RED today (the
    call site omits fail_closed -> missing key defaults permitted)."""
    ds = DataSource(id="ds1", name="Prod", source_type="postgres")
    session = _delete_session([], ds)
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))

    delete_source = _handler("DELETE", "/datasources")
    with pytest.raises(HTTPException) as exc:
        delete_source(request=_req_missing_key("bob"), ds_id="ds1")
    assert exc.value.status_code == 403


def test_create_source_denies_on_lookup_exception_fail_closed(monkeypatch):
    """Lookup-raise fail-open branch: get_privileges() raising must DENY
    (403) once create_source opts into fail_closed=True. RED today."""
    session = MagicMock()
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))
    create = _handler("POST", "/datasources")
    body = mod.DataSourceCreate(name="Prod", source_type="postgres", host="h",
                                port=5432, database="d", username="u", password="p")
    with pytest.raises(HTTPException) as exc:
        create(request=_req_lookup_raises("bob"), body=body)
    assert exc.value.status_code == 403


def test_delete_source_empty_user_still_permitted_d08(monkeypatch):
    """D-08: with AUTH_ENABLED=false and an empty current_user (single-user
    / auth-disabled owner), delete_source stays permitted (NOT 403) even
    under fail_closed=True -- require_privilege's empty-user early return
    is unchanged. Passes both before and after Task 2."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    ds = DataSource(id="ds1", name="Prod", source_type="postgres")
    session = _delete_session([], ds)
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))
    req = SimpleNamespace(
        state=SimpleNamespace(current_user=""),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        client=SimpleNamespace(host="10.0.0.9"),
    )

    delete_source = _handler("DELETE", "/datasources")
    resp = delete_source(request=req, ds_id="ds1")
    assert resp == {"status": "deleted"}
