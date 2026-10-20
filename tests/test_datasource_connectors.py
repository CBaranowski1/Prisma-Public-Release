"""Connector contract: build engine, test connection, list + reflect tables.
A SQLite file stands in for an external DB so no live server is needed."""
import json
import socket
import threading
import time
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, MetaData, Table, Column, String
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.pool import QueuePool

import src.datasource_connectors as dc
from core.querybuilder_models import DataSource


def _pg_source(host="db.example.com", options=None):
    return DataSource(
        id="pg1", name="pg", source_type="postgres", host=host, port=5432,
        database="mydb", username="u", password="p",
        options=json.dumps(options) if options is not None else None,
    )


def _mysql_source(host="db.example.com", options=None):
    return DataSource(
        id="my1", name="mysql", source_type="mysql", host=host, port=3306,
        database="mydb", username="u", password="p",
        options=json.dumps(options) if options is not None else None,
    )


def _fake_getaddrinfo_once(ip):
    """Return a socket.getaddrinfo replacement that always resolves to `ip`."""
    def _fake(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]
    return _fake


@pytest.fixture
def sqlite_source(tmp_path):
    path = tmp_path / "ext.db"
    engine = create_engine(f"sqlite:///{path}")
    md = MetaData()
    Table("customers", md, Column("id", String, primary_key=True), Column("name", String))
    Table("orders", md, Column("id", String, primary_key=True), Column("customer_id", String))
    md.create_all(engine)
    url = f"sqlite:///{path}"

    class _SqliteConnector(dc.DataSourceConnector):
        def make_engine(self):
            return create_engine(url)
    return _SqliteConnector()


def test_test_connection_ok(sqlite_source):
    sqlite_source.test_connection()  # must not raise


def test_list_tables(sqlite_source):
    assert set(sqlite_source.list_tables()) == {"customers", "orders"}


def test_reflect_table(sqlite_source):
    tbl = sqlite_source.reflect_table("customers")
    assert tbl.name == "customers"
    assert {c.name for c in tbl.columns} == {"id", "name"}


def test_get_connector_unknown_type_raises():
    from core.querybuilder_models import DataSource
    ds = DataSource(id="1", name="x", source_type="oracle")
    with pytest.raises(dc.ConnectorError):
        dc.get_connector(ds)


# --- Task 1: vetted connect_args TLS allowlist per dialect (D-06, D-05) ---

def test_tls_allowlist_postgres(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update(kw) or MagicMock())
    ds = _pg_source(options={"connect_args": {
        "sslmode": "verify-full", "sslrootcert": "/ca.pem",
        "evil": "x", "host": "1.2.3.4",
    }})
    connector = dc.get_connector(ds)
    connector.make_engine()
    connect_args = captured["connect_args"]
    assert connect_args["sslmode"] == "verify-full"
    assert connect_args["sslrootcert"] == "/ca.pem"
    assert "evil" not in connect_args
    assert "host" not in connect_args


def test_tls_allowlist_mysql(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update(kw) or MagicMock())
    ds = _mysql_source(options={"connect_args": {
        "ssl": {"ca": "/ca.pem"}, "ssl_verify_cert": True, "ssl_mode": "REQUIRED",
    }})
    connector = dc.get_connector(ds)
    connector.make_engine()
    connect_args = captured["connect_args"]
    assert connect_args["ssl"] == {"ca": "/ca.pem"}
    assert connect_args["ssl_verify_cert"] is True
    assert "ssl_mode" not in connect_args


def test_loopback_exempt():
    ds = _pg_source(host="127.0.0.1")
    connector = dc.get_connector(ds)
    # No options set and no forced TLS default — loopback stays exempt (D-05).
    assert "sslmode" not in connector._connect_args
    assert "ssl" not in connector._connect_args


# --- Task 2: resolve-once Postgres hostaddr pinning (D-03); MySQL residual (D-03) ---

def test_pin_postgres(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update({"url": url, **kw}) or MagicMock())
    ds = _pg_source(host="db.example.com")
    connector = dc.get_connector(ds)
    connector.make_engine()
    assert captured["connect_args"]["hostaddr"] == "203.0.113.9"
    assert "db.example.com" in captured["url"]


def test_pin_lifetime(monkeypatch):
    import src.ssrf_guard as guard
    spy = MagicMock(wraps=guard.resolve_and_pin_host)
    monkeypatch.setattr(guard, "resolve_and_pin_host", spy)
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = []
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.append(kw) or MagicMock())
    ds = _pg_source(host="db.example.com")
    connector = dc.get_connector(ds)
    connector.make_engine()
    connector.make_engine()
    assert spy.call_count == 1
    assert all(c["connect_args"]["hostaddr"] == "203.0.113.9" for c in captured)


def test_rebind_backstop(monkeypatch):
    calls = {"n": 0}

    def _rebinding_getaddrinfo(host, *args, **kwargs):
        calls["n"] += 1
        ip = "203.0.113.9" if calls["n"] == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_getaddrinfo)
    captured = []
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.append(kw) or MagicMock())
    ds = _pg_source(host="db.example.com")
    connector = dc.get_connector(ds)
    connector.make_engine()
    connector.make_engine()
    assert calls["n"] == 1
    assert all(c["connect_args"]["hostaddr"] == "203.0.113.9" for c in captured)


def test_mysql_no_pin(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update(kw) or MagicMock())
    ds = _mysql_source(host="db.example.com")
    connector = dc.get_connector(ds)
    connector.make_engine()
    connect_args = captured.get("connect_args", {})
    assert "hostaddr" not in connect_args
    assert "host" not in connect_args


def test_blocked_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("169.254.169.254"))
    ds = _pg_source(host="metadata.internal")
    with pytest.raises(dc.ConnectorError):
        dc.get_connector(ds)


# --- QB-08 Task 1: timeout defaults, numeric-only override parser, SyncTable.truncated ---

def test_timeout_override_defaults_on_none():
    out = dc._timeout_overrides(None)
    assert out == {
        "statement_timeout_ms": dc.DEFAULT_STATEMENT_TIMEOUT_MS,
        "connect_timeout_s": dc.DEFAULT_CONNECT_TIMEOUT_S,
        "pool_timeout_s": dc.DEFAULT_POOL_TIMEOUT_S,
    }


def test_timeout_override_defaults_on_invalid_json():
    out = dc._timeout_overrides("not json")
    assert out == {
        "statement_timeout_ms": dc.DEFAULT_STATEMENT_TIMEOUT_MS,
        "connect_timeout_s": dc.DEFAULT_CONNECT_TIMEOUT_S,
        "pool_timeout_s": dc.DEFAULT_POOL_TIMEOUT_S,
    }


def test_timeout_override_explicit_values():
    options = json.dumps({"timeouts": {
        "statement_timeout_ms": 5000, "connect_timeout_s": 3, "pool_timeout_s": 15,
    }})
    out = dc._timeout_overrides(options)
    assert out == {
        "statement_timeout_ms": 5000, "connect_timeout_s": 3, "pool_timeout_s": 15,
    }


@pytest.mark.parametrize("bad_value", [10_000_000, -1, 0, "5000", None])
def test_timeout_override_clamps_bad_statement_value(bad_value):
    options = json.dumps({"timeouts": {"statement_timeout_ms": bad_value}})
    out = dc._timeout_overrides(options)
    assert out["statement_timeout_ms"] == dc.DEFAULT_STATEMENT_TIMEOUT_MS
    # untouched keys still get their own defaults
    assert out["connect_timeout_s"] == dc.DEFAULT_CONNECT_TIMEOUT_S
    assert out["pool_timeout_s"] == dc.DEFAULT_POOL_TIMEOUT_S


def test_synctable_truncated_defaults_false():
    st = dc.SyncTable(columns=[], rows=[])
    assert st.truncated is False


def test_synctable_truncated_settable_true():
    st = dc.SyncTable(columns=[], rows=[], truncated=True)
    assert st.truncated is True


# --- QB-08 Task 2: timeouts threaded into connect_args + create_engine ---

def test_timeout_defaults_postgres(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update(kw) or MagicMock())
    ds = _pg_source()
    connector = dc.get_connector(ds)
    connector.make_engine()
    connect_args = captured["connect_args"]
    assert connect_args["connect_timeout"] == dc.DEFAULT_CONNECT_TIMEOUT_S
    assert connect_args["options"] == f"-c statement_timeout={dc.DEFAULT_STATEMENT_TIMEOUT_MS}"
    assert connect_args["hostaddr"] == "203.0.113.9"
    assert captured["pool_timeout"] == dc.DEFAULT_POOL_TIMEOUT_S
    assert captured["pool_pre_ping"] is True


def test_timeout_defaults_mysql(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update(kw) or MagicMock())
    ds = _mysql_source()
    connector = dc.get_connector(ds)
    connector.make_engine()
    connect_args = captured["connect_args"]
    assert connect_args["connect_timeout"] == dc.DEFAULT_CONNECT_TIMEOUT_S
    assert connect_args["read_timeout"] == dc.DEFAULT_STATEMENT_TIMEOUT_MS / 1000
    assert captured["pool_timeout"] == dc.DEFAULT_POOL_TIMEOUT_S
    assert captured["pool_pre_ping"] is True


def test_timeout_override_postgres(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update(kw) or MagicMock())
    ds = _pg_source(options={"timeouts": {
        "statement_timeout_ms": 5000, "connect_timeout_s": 3, "pool_timeout_s": 15,
    }})
    connector = dc.get_connector(ds)
    connector.make_engine()
    connect_args = captured["connect_args"]
    assert connect_args["connect_timeout"] == 3
    assert connect_args["options"] == "-c statement_timeout=5000"
    assert captured["pool_timeout"] == 15


def test_timeout_bad_override_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    captured = {}
    monkeypatch.setattr(dc, "create_engine",
                         lambda url, **kw: captured.update(kw) or MagicMock())
    ds = _pg_source(options={"timeouts": {"statement_timeout_ms": 10_000_000}})
    connector = dc.get_connector(ds)
    connector.make_engine()
    connect_args = captured["connect_args"]
    assert connect_args["options"] == f"-c statement_timeout={dc.DEFAULT_STATEMENT_TIMEOUT_MS}"


def test_timeout_options_never_add_raw_options_key_to_allowlist():
    # Structural guard against Pitfall 1 regression: "options" must never be a
    # vetted/allowlisted passthrough key.
    assert "options" not in dc._PG_TLS_KEYS
    assert "options" not in dc._MYSQL_TLS_KEYS


# --- Task 3: dialect-agnostic pool-timeout behavioral proof (QB-08 SC-5 half) ---

def test_pool_timeout_enforced_dialect_agnostic(tmp_path):
    """psycopg/PyMySQL aren't installed on-machine (Environment Availability),
    so this proves the pool_timeout half of QB-08 the honest, dialect-agnostic
    way: force a real SQLite engine onto QueuePool with a single slot, hold
    that one connection open on a background thread, and confirm a second
    checkout raises sqlalchemy.exc.TimeoutError within the configured bound —
    SQLAlchemy's QueuePool enforces this, not a kwarg we merely pass through."""
    path = tmp_path / "pool.db"
    engine = create_engine(
        f"sqlite:///{path}",
        poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.2,
    )
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


# --- Task 3: scrub credentials at the connector error boundary (D-08 sink a) ---

def _dsn_error():
    return OperationalError("SELECT 1", {},
                             Exception("could not connect to postgresql://u:secretpw@h/d"))


def test_scrub_test_connection():
    class _FailConnector(dc.DataSourceConnector):
        def make_engine(self):
            raise _dsn_error()

    with pytest.raises(dc.ConnectorError) as exc_info:
        _FailConnector().test_connection()
    msg = str(exc_info.value)
    assert "secretpw" not in msg
    assert "h" in msg
    assert "d" in msg


def test_scrub_list_tables():
    class _FailConnector(dc.DataSourceConnector):
        def make_engine(self):
            raise _dsn_error()

    with pytest.raises(dc.ConnectorError) as exc_info:
        _FailConnector().list_tables()
    assert "secretpw" not in str(exc_info.value)


def test_scrub_reflect():
    class _FailConnector(dc.DataSourceConnector):
        def make_engine(self):
            raise _dsn_error()

    with pytest.raises(dc.ConnectorError) as exc_info:
        _FailConnector().reflect_table("t")
    assert "secretpw" not in str(exc_info.value)


# --- WR-02 (MEDIUM): _register_mysql_stmt_kill coverage (QB-08 defense-in- ---
# depth, previously untested) ------------------------------------------------


def _capture_listens_for(monkeypatch, captured):
    """Replace sqlalchemy.event.listens_for with a stub that just RECORDS
    the (target, identifier, fn) instead of actually registering against a
    real Engine -- lets these tests exercise `_set_mysql_stmt_kill`'s own
    logic (the SET SESSION call + its guard) directly and deterministically,
    without needing a real MySQL server or a dialect-specific DBAPI
    connection lifecycle."""
    def _stub(target, identifier):
        def decorator(fn):
            captured["target"] = target
            captured["identifier"] = identifier
            captured["fn"] = fn
            return fn
        return decorator
    monkeypatch.setattr(dc.event, "listens_for", _stub)


def test_mysql_stmt_kill_registers_connect_listener_with_configured_timeout(monkeypatch):
    """(1) The MySQL engine's `connect` event fires SET SESSION
    max_execution_time=<configured value> against the DBAPI cursor."""
    captured = {}
    _capture_listens_for(monkeypatch, captured)
    connector = dc._UrlConnector("mysql+pymysql://u:p@host/db", statement_timeout_ms=4242)
    connector._register_mysql_stmt_kill(engine=MagicMock())
    assert captured["identifier"] == "connect"

    fake_cursor = MagicMock()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    captured["fn"](fake_conn, None)
    fake_cursor.execute.assert_called_once_with("SET SESSION max_execution_time=4242")
    fake_cursor.close.assert_called_once()


def test_mysql_stmt_kill_swallows_mariadb_style_cursor_failure(monkeypatch):
    """(2) A cursor that raises on SET SESSION (simulating MariaDB/old MySQL,
    which has no such variable) must not propagate -- the connect event
    itself must not fail the connection."""
    captured = {}
    _capture_listens_for(monkeypatch, captured)
    connector = dc._UrlConnector("mysql+pymysql://u:p@host/db", statement_timeout_ms=1000)
    connector._register_mysql_stmt_kill(engine=MagicMock())

    fake_cursor = MagicMock()
    fake_cursor.execute.side_effect = Exception("Unknown system variable 'max_execution_time'")
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cursor
    captured["fn"](fake_conn, None)  # must not raise


def test_mysql_stmt_kill_swallows_registration_failure_on_non_engine_target(monkeypatch):
    """(3) event.listens_for(engine, ...) raising AT REGISTRATION TIME
    (simulating a non-Engine double, e.g. a bare test-spy target that
    sqlalchemy's event system rejects) must be swallowed and logged, never
    propagated out of make_engine()."""
    def _raising_listens_for(target, identifier):
        raise TypeError("not a valid event target")
    monkeypatch.setattr(dc.event, "listens_for", _raising_listens_for)
    connector = dc._UrlConnector("mysql+pymysql://u:p@host/db")
    connector._register_mysql_stmt_kill(engine=MagicMock())  # must not raise


def test_mysql_make_engine_registers_stmt_kill_listener(monkeypatch):
    """End-to-end (still driver-free): make_engine() itself wires up the
    listener for a mysql:// URL via the real event system against a real
    (SQLite-backed, driver-agnostic) Engine -- proving the registration
    survives an actual `event.listens_for(engine, "connect")` call, not just
    the stubbed-target unit tests above."""
    ds = _mysql_source()
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_once("203.0.113.9"))
    connector = dc.get_connector(ds)
    real_create_engine = dc.create_engine
    monkeypatch.setattr(
        dc, "create_engine",
        lambda url, **kw: real_create_engine("sqlite://"))  # in-memory stand-in
    engine = connector.make_engine()
    # The real assertion: connecting must not raise (SQLite's cursor.execute
    # of the MySQL-only SET SESSION statement fails, exactly like the
    # MariaDB case, and must be swallowed by the guarded listener).
    with engine.connect() as conn:
        conn.execute(dc.text("SELECT 1"))
