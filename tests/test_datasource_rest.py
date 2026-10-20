"""REST connector: GET an endpoint, normalize a JSON array into rows + columns.
httpx is monkeypatched so no network is used, EXCEPT the redirect/pin tests
below (13-04), which spin up a real local loopback server -- no external
network is ever touched."""
import http.server
import inspect
import json
import logging
import socket
import threading

import httpcore
import httpx
import pytest

import src.datasource_rest as rest
from core.querybuilder_models import DataSource


@pytest.fixture(autouse=True)
def _mock_dns(monkeypatch):
    # "x.test" is an IANA-reserved never-resolving TLD (RFC 6761) used as a
    # fixture host here; the ssrf_guard (13-02, D-02) now fails CLOSED on an
    # unresolvable host, so these connector-logic tests (which don't exercise
    # DNS) need a deterministic mocked resolution to a public IP.
    # Host-aware (13-04): a literal IP address (e.g. "127.0.0.1", used by the
    # redirect/pin tests' real local loopback servers) passes through to the
    # REAL socket.getaddrinfo -- those tests need a genuine local socket
    # connection, not a fake one. Only non-IP symbolic hostnames get the
    # deterministic mocked public IP.
    real_getaddrinfo = socket.getaddrinfo

    def _fake_getaddrinfo(host, *a, **k):
        try:
            import ipaddress
            ipaddress.ip_address(host)
        except (ValueError, TypeError):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


class _CtxResp:
    """Minimal context-manager wrapper (CR-02) so a fake client's `.stream()`
    can hand back an already-built resp object (e.g. a raw `httpx.Response`)
    matching `with client.stream(...) as resp:`'s shape, without needing a
    real streaming implementation in these unit tests. `raise_for_status()`
    on the wrapped resp always runs BEFORE any `iter_bytes()` call, so error
    doubles (a bare non-2xx httpx.Response with no body) never need
    `iter_bytes()` defined at all."""
    def __init__(self, resp): self._resp = resp
    def __enter__(self): return self._resp
    def __exit__(self, *a): return False


class _FakeResp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p
    # CR-02 (HIGH, QB-17): the non-paginated branch now reads via
    # `client.stream(...)` + `resp.iter_bytes()` rather than `client.get()` +
    # `.content`/`.json()` -- this fake doubles as its own "streaming"
    # response (`with client.stream(...) as resp:` just needs `__enter__` to
    # hand back something with `raise_for_status()`/`iter_bytes()`).
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_bytes(self):
        yield json.dumps(self._p).encode("utf-8")


class _FakeClient:
    def __init__(self, payload): self._p = payload
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, headers=None): return _FakeResp(self._p)
    def stream(self, method, url, params=None, headers=None): return _FakeResp(self._p)


def test_rest_fetch_array(monkeypatch):
    ds = DataSource(id="s1", name="api", source_type="rest",
                    options=json.dumps({"base_url": "https://x.test",
                                        "endpoints": {"users": {"path": "/users"}}}))
    payload = [{"id": 1, "name": "A", "meta": {"k": "v"}}, {"id": 2, "name": "B"}]
    monkeypatch.setattr(rest, "_client", lambda ds: _FakeClient(payload))
    conn = rest.RestConnector(ds)
    assert conn.list_tables() == ["users"]
    st = conn.fetch_table("users")
    names = {c.name for c in st.columns}
    assert {"id", "name", "meta"} <= names
    rows = list(st.rows)
    # Nested object JSON-stringified into a string cell.
    assert isinstance(rows[0]["meta"], str) and "\"k\"" in rows[0]["meta"]


def test_rest_unknown_endpoint(monkeypatch):
    ds = DataSource(id="s2", name="api", source_type="rest",
                    options=json.dumps({"base_url": "https://x.test", "endpoints": {}}))
    conn = rest.RestConnector(ds)
    with pytest.raises(rest.ConnectorError):
        conn.fetch_table("missing")


# --- Task 1 (13-04, D-09/SEC-14): redirects blocked + translated ----------


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Always answers 302 -> an off-host location. Never actually reachable
    (no external network involved) -- only used to exercise the httpx
    redirect-response code path."""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", "http://example.invalid/elsewhere")
        self.end_headers()

    def log_message(self, *a, **k):  # silence stderr request logging
        pass


def _start_redirect_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


def test_redirect_test_connection_becomes_connector_error():
    """A 302 from test_connection()'s GET must surface as a ConnectorError
    mentioning 'redirect' -- never an unhandled httpx.HTTPStatusError, never
    a followed request (RESEARCH Pitfall 2)."""
    server, port = _start_redirect_server()
    try:
        ds = DataSource(id="s3", name="api", source_type="rest",
                        options=json.dumps({"base_url": f"http://127.0.0.1:{port}",
                                             "endpoints": {}}))
        conn = rest.RestConnector(ds)
        with pytest.raises(rest.ConnectorError, match="redirect"):
            conn.test_connection()
    finally:
        server.shutdown()


def test_redirect_fetch_becomes_connector_error():
    """Same 3xx-to-ConnectorError translation through the (non-paginated)
    _fetch_records branch."""
    server, port = _start_redirect_server()
    try:
        ds = DataSource(id="s4", name="api", source_type="rest",
                        options=json.dumps({
                            "base_url": f"http://127.0.0.1:{port}",
                            "endpoints": {"users": {"path": "/users"}}}))
        conn = rest.RestConnector(ds)
        with pytest.raises(rest.ConnectorError, match="redirect"):
            conn.fetch_table("users")
    finally:
        server.shutdown()


def test_client_sets_follow_redirects_false_explicitly():
    """The constructed httpx.Client must set follow_redirects=False
    explicitly (D-09) -- not rely on httpx's own version-dependent default."""
    ds = DataSource(id="s5", name="api", source_type="rest",
                    options=json.dumps({"base_url": "https://x.test", "endpoints": {}}))
    with rest._client(ds) as c:
        assert c.follow_redirects is False


# --- Task 2 (13-04, D-03/SEC-14): pin the resolved IP for the REST connect -


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    """Answers 200 with an empty JSON array; records the Host header it
    actually received on the class (so a fresh instance per request doesn't
    lose it)."""

    received_host = None

    def do_GET(self):
        type(self).received_host = self.headers.get("Host")
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **k):
        pass


@pytest.fixture
def _pin_server():
    _CapturingHandler.received_host = None
    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server, port
    server.shutdown()


_GUARDED_HOST = "pinned-host.test"


def _rebinding_getaddrinfo(guarded_host, first_ip, rebind_ip, call_counter):
    """Answers `first_ip` the first time `guarded_host` is resolved, then
    `rebind_ip` on every subsequent resolution -- simulating a DNS-rebind
    attacker who changes the answer after the guard's initial check. Any
    OTHER host (e.g. a literal IP used for the real TCP connect once pinned)
    passes through unaffected."""
    real_getaddrinfo = socket.getaddrinfo

    def _fake(host, *a, **k):
        if host == guarded_host:
            call_counter["n"] += 1
            ip = first_ip if call_counter["n"] == 1 else rebind_ip
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
        return real_getaddrinfo(host, *a, **k)

    return _fake


def test_rest_pin_connects_to_pinned_ip(monkeypatch, _pin_server):
    """Even though a 2nd DNS resolution for the guarded host would answer a
    DIFFERENT IP (simulated rebind), the connector still connects to the
    FIRST-resolved (pinned) IP -- the real local server receives the
    request, proving resolve-once + pin (RESEARCH Pattern 2)."""
    server, port = _pin_server
    calls = {"n": 0}
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _rebinding_getaddrinfo(_GUARDED_HOST, "127.0.0.1", "203.0.113.9", calls))
    ds = DataSource(id="s6", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": f"http://{_GUARDED_HOST}:{port}",
                        "endpoints": {"users": {"path": "/users"}}}))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    assert list(st.rows) == []  # empty JSON array from the real server
    assert _CapturingHandler.received_host is not None  # request actually landed


def test_rest_pin_connects_to_pinned_ip_uppercase_host(monkeypatch, _pin_server):
    """CR-01 regression: a base_url whose host contains uppercase/mixed-case
    letters must still get pinned. httpx/httpcore lowercase the connect-target
    host before handing it to _PinnedBackend.connect_tcp; if `_guarded_host`
    were stored with its original (un-lowercased) casing, the equality check
    in connect_tcp would never match, silently falling through to `else host`
    -- a fresh, rebindable DNS resolution for the real backend's own connect,
    reopening the exact TOCTOU window the pin exists to close. Otherwise
    identical to test_rest_pin_connects_to_pinned_ip above, just with a
    mixed-case host."""
    server, port = _pin_server
    calls = {"n": 0}
    mixed_case_host = "Pinned-Host.test"
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _rebinding_getaddrinfo(_GUARDED_HOST, "127.0.0.1", "203.0.113.9", calls))
    ds = DataSource(id="s11", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": f"http://{mixed_case_host}:{port}",
                        "endpoints": {"users": {"path": "/users"}}}))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    assert list(st.rows) == []  # empty JSON array from the real server
    assert _CapturingHandler.received_host is not None  # request actually landed
    # Resolved exactly once (the pin) -- a second resolution would mean the
    # pin fell through and the (simulated) rebind IP was used instead.
    assert calls["n"] == 1


def test_rest_pin_host_header_preserved(monkeypatch, _pin_server):
    """The Host header the real server receives reflects the ORIGINAL
    hostname from the configured base_url, not the pinned IP."""
    server, port = _pin_server
    calls = {"n": 0}
    monkeypatch.setattr(
        socket, "getaddrinfo",
        _rebinding_getaddrinfo(_GUARDED_HOST, "127.0.0.1", "203.0.113.9", calls))
    ds = DataSource(id="s7", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": f"http://{_GUARDED_HOST}:{port}",
                        "endpoints": {"users": {"path": "/users"}}}))
    conn = rest.RestConnector(ds)
    conn.fetch_table("users")
    assert _CapturingHandler.received_host == f"{_GUARDED_HOST}:{port}"


def test_rest_pin_resolved_once(monkeypatch):
    """resolve_and_pin_host is invoked exactly once per RestConnector
    construction, never per request -- re-resolving would reopen the
    DNS-rebind TOCTOU window (RESEARCH Pattern 1's lifetime pitfall,
    mirrored here for REST)."""
    from unittest.mock import MagicMock
    real = rest.resolve_and_pin_host
    spy = MagicMock(wraps=real)
    monkeypatch.setattr(rest, "resolve_and_pin_host", spy)
    ds = DataSource(id="s8", name="api", source_type="rest",
                    options=json.dumps({"base_url": "https://x.test", "endpoints": {}}))
    rest.RestConnector(ds)
    assert spy.call_count == 1


def test_rest_pin_blocked_host_raises_connector_error(monkeypatch):
    """A base_url host that resolves to a blocked (metadata) address raises
    ConnectorError -- SsrfBlocked is translated, never escapes raw."""
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                 ("169.254.169.254", 0))])
    ds = DataSource(id="s9", name="api", source_type="rest",
                    options=json.dumps({"base_url": "http://metadata.test",
                                         "endpoints": {}}))
    with pytest.raises(rest.ConnectorError):
        rest.RestConnector(ds)


def test_rest_no_pin_for_loopback_base_url():
    """A literally-loopback base_url (127.0.0.1/localhost) is not pinned --
    there is no rebind risk connecting to the local machine."""
    ds = DataSource(id="s10", name="api", source_type="rest",
                    options=json.dumps({"base_url": "http://127.0.0.1:9",
                                         "endpoints": {}}))
    conn = rest.RestConnector(ds)
    assert getattr(ds, "_rest_pinned_host_ip", None) is None
    assert conn is not None  # constructed without attempting any DNS pin


# --- Task 3 (13-04): httpcore contract guard-rail --------------------------


def test_httpcore_pinning_contract_guard_rail():
    """Fails loudly if a future unpinned httpx/httpcore bump breaks the
    NetworkBackend extension point PinnedHTTPTransport depends on, instead
    of silently reopening the REST DNS-rebind TOCTOU window (RESEARCH
    Pattern 2's version-portability risk note)."""
    pool_params = inspect.signature(httpcore.ConnectionPool.__init__).parameters
    assert "network_backend" in pool_params
    connect_params = inspect.signature(httpcore.NetworkBackend.connect_tcp).parameters
    assert "host" in connect_params
    assert "port" in connect_params


# --- 23-02 (QB-09, D-05/D-06/D-07/D-08): options.auth block --------------


def test_auth_bearer_backcompat_with_password():
    """Absent options.auth reproduces today's exact behavior byte-for-byte:
    Bearer from ds.password when present (D-07)."""
    ds = DataSource(id="a1", name="api", source_type="rest",
                    password="secret-token",
                    options=json.dumps({"base_url": "https://x.test", "endpoints": {}}))
    rest.RestConnector(ds)
    with rest._client(ds) as c:
        assert c.headers.get("Authorization") == "Bearer secret-token"


def test_auth_bearer_backcompat_no_password():
    """Absent options.auth AND no password: no Authorization header at all
    (D-07 back-compat, the other branch of today's behavior)."""
    ds = DataSource(id="a2", name="api", source_type="rest",
                    options=json.dumps({"base_url": "https://x.test", "endpoints": {}}))
    rest.RestConnector(ds)
    with rest._client(ds) as c:
        assert "Authorization" not in c.headers


def test_auth_api_key_header(monkeypatch):
    """auth.type == 'api_key' with a configured header name injects that
    header carrying ds.password and omits Authorization: Bearer (D-05)."""
    ds = DataSource(id="a3", name="api", source_type="rest",
                    password="key-123",
                    options=json.dumps({
                        "base_url": "https://x.test", "endpoints": {},
                        "auth": {"type": "api_key", "header": "X-API-Key"}}))
    rest.RestConnector(ds)
    with rest._client(ds) as c:
        assert c.headers.get("X-API-Key") == "key-123"
        assert "Authorization" not in c.headers


def test_auth_header_type_is_api_key_alias():
    """auth.type == 'header' behaves identically to 'api_key' -- both are
    "a configurable header name carrying the secret" per D-05."""
    ds = DataSource(id="a4", name="api", source_type="rest",
                    password="key-456",
                    options=json.dumps({
                        "base_url": "https://x.test", "endpoints": {},
                        "auth": {"type": "header", "header": "X-Custom-Auth"}}))
    rest.RestConnector(ds)
    with rest._client(ds) as c:
        assert c.headers.get("X-Custom-Auth") == "key-456"
        assert "Authorization" not in c.headers


def test_auth_basic_builds_httpx_basic_auth():
    """auth.type == 'basic' builds the client with httpx.BasicAuth(username,
    password) -- no manual base64 (D-05)."""
    ds = DataSource(id="a5", name="api", source_type="rest",
                    username="alice", password="s3cret",
                    options=json.dumps({
                        "base_url": "https://x.test", "endpoints": {},
                        "auth": {"type": "basic"}}))
    rest.RestConnector(ds)
    with rest._client(ds) as c:
        assert isinstance(c.auth, httpx.BasicAuth)
        # httpx.BasicAuth applies the header per-request via its auth flow,
        # not as a static client header -- confirming no manual header was set.
        assert "Authorization" not in c.headers


def test_auth_basic_missing_username_raises_connector_error():
    """WR-01 (MEDIUM): auth.type == 'basic' with no username configured must
    raise a clean ConnectorError (caught by the /test, /tables, /upload
    routes -> 400) instead of an unhandled httpx.BasicAuth(None, ...)
    TypeError -> raw 500 (fail-before/pass-after: httpx.BasicAuth(None,
    'secret') itself raises TypeError, confirmed at review time)."""
    ds = DataSource(id="a7", name="api", source_type="rest",
                    password="s3cret",  # no username
                    options=json.dumps({
                        "base_url": "https://x.test", "endpoints": {},
                        "auth": {"type": "basic"}}))
    rest.RestConnector(ds)
    with pytest.raises(rest.ConnectorError, match="basic"):
        rest._client(ds)


def test_auth_options_hold_no_secret_value():
    """options.auth holds only non-secret metadata (type/header name) --
    the secret itself is never read from options (D-06)."""
    ds = DataSource(id="a6", name="api", source_type="rest",
                    password="should-not-appear-in-options",
                    options=json.dumps({
                        "base_url": "https://x.test", "endpoints": {},
                        "auth": {"type": "api_key", "header": "X-API-Key"}}))
    conn = rest.RestConnector(ds)
    assert "should-not-appear-in-options" not in json.dumps(conn._auth)


# --- 23-02 (QB-09, D-08 + Pitfall 3): query-param auth on every request ----


class _QueryRespShim:
    """Mirrors the module's own `_Resp`-style test doubles -- a bare
    raise_for_status()/json() pair (used by the query-auth page/next/
    test_connection call-recording clients below). Also usable as its own
    `client.stream(...)` context manager (CR-02) for the single-GET
    no-pagination variant below."""
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_bytes(self):
        yield json.dumps(self._p).encode("utf-8")


def test_auth_query_param_every_page_page_mode(monkeypatch):
    """auth.type == 'query' carries the configured param on EVERY page in
    `page` pagination mode, merged alongside (not replacing) the pagination
    param (D-08)."""
    calls = []

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            calls.append(dict(params or {}))
            page = int((params or {}).get("page", 1))
            data = {1: [{"id": 1}], 2: []}.get(page, [])
            return _QueryRespShim(data)

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="q1", name="api", source_type="rest", password="qsecret",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {
                            "path": "/users",
                            "pagination": {"type": "page", "param": "page", "max_pages": 5}}},
                        "auth": {"type": "query", "param": "api_key"}}))
    conn = rest.RestConnector(ds)
    list(conn.fetch_table("users").rows)
    assert len(calls) >= 2
    for call_params in calls:
        assert call_params.get("api_key") == "qsecret"
        assert "page" in call_params  # pagination param not dropped


def test_auth_query_param_every_page_next_mode(monkeypatch):
    """Same guarantee in `next`-link pagination mode."""
    calls = []

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            calls.append((url, dict(params or {})))
            if url.endswith("/p2"):
                return _QueryRespShim({"items": [{"id": 2}], "next": None})
            return _QueryRespShim({"items": [{"id": 1}], "next": "https://x.test/p2"})

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="q2", name="api", source_type="rest", password="qsecret2",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {
                            "path": "/users", "records_path": "items",
                            "pagination": {"type": "next", "next_field": "next", "max_pages": 5}}},
                        "auth": {"type": "query", "param": "token"}}))
    conn = rest.RestConnector(ds)
    list(conn.fetch_table("users").rows)
    assert len(calls) == 2
    for _, params in calls:
        assert params.get("token") == "qsecret2"


def test_auth_query_param_test_connection(monkeypatch):
    """Same guarantee on test_connection()'s GET (D-08)."""
    calls = []

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            calls.append(dict(params or {}))
            return _QueryRespShim([])

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="q3", name="api", source_type="rest", password="qsecret3",
                    options=json.dumps({
                        "base_url": "https://x.test", "endpoints": {},
                        "auth": {"type": "query", "param": "api_key"}}))
    conn = rest.RestConnector(ds)
    conn.test_connection()
    assert calls[0].get("api_key") == "qsecret3"


def test_auth_query_param_single_get_no_pagination(monkeypatch):
    """Same guarantee on the non-paginated single-GET branch of
    _fetch_records (D-08)."""
    calls = []

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            calls.append(dict(params or {}))
            return _QueryRespShim([{"id": 1}])
        def stream(self, method, url, params=None, headers=None):
            calls.append(dict(params or {}))
            return _QueryRespShim([{"id": 1}])

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="q5", name="api", source_type="rest", password="qsecret5",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {"path": "/users"}},
                        "auth": {"type": "query", "param": "api_key"}}))
    conn = rest.RestConnector(ds)
    list(conn.fetch_table("users").rows)
    assert calls[0].get("api_key") == "qsecret5"


def test_query_secret_not_leaked_in_error_message(monkeypatch):
    """Pitfall 3 (RESEARCH): httpx.HTTPStatusError.__str__() embeds the full
    request URL including query string. A 500 from a query-auth-configured
    endpoint must NOT leak the secret value into the raised ConnectorError's
    message -- built from status_code/reason_phrase only, never str(e)/the
    URL."""
    secret_url = "https://x.test/users?api_key=SUPERSECRET"

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            return httpx.Response(500, request=httpx.Request("GET", secret_url))
        def stream(self, method, url, params=None, headers=None):
            return _CtxResp(httpx.Response(500, request=httpx.Request("GET", secret_url)))

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="q4", name="api", source_type="rest", password="SUPERSECRET",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {"path": "/users"}},
                        "auth": {"type": "query", "param": "api_key"}}))
    conn = rest.RestConnector(ds)
    with pytest.raises(rest.ConnectorError) as exc_info:
        conn.fetch_table("users")
    assert "SUPERSECRET" not in str(exc_info.value)


# --- 23-04 (QB-17, D-15/D-17): _fetch_records converted to a true generator ---


def test_fetch_table_rows_is_a_generator_not_list(monkeypatch):
    """QB-17/D-15: fetch_table(...).rows must be a lazy generator/iterator,
    never a materialized list (no __len__), even though every row is still
    reachable by iterating it fully."""
    ds = DataSource(id="g1", name="api", source_type="rest",
                    options=json.dumps({"base_url": "https://x.test",
                                        "endpoints": {"users": {"path": "/users"}}}))
    payload = [{"id": 1}, {"id": 2}, {"id": 3}]
    monkeypatch.setattr(rest, "_client", lambda ds: _FakeClient(payload))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    assert not hasattr(st.rows, "__len__")
    assert [r["id"] for r in st.rows] == [1, 2, 3]


def test_fetch_table_stream_yields_every_row_across_pages(monkeypatch):
    """A generator-based REST stream must still yield every row across
    multiple pages -- not just the bounded first sample (D-17) -- and the
    inferred columns must still reflect the sampled rows' keys."""
    class _PagedClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            page = int((params or {}).get("page", 1))
            data = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}], 3: []}.get(page, [])
            return _FakeResp(data)

    monkeypatch.setattr(rest, "_client", lambda ds: _PagedClient())
    ds = DataSource(id="g2", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {
                            "path": "/users",
                            "pagination": {"type": "page", "param": "page", "max_pages": 10}}}}))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    assert {c.name for c in st.columns} == {"id"}
    assert [r["id"] for r in st.rows] == [1, 2, 3]


def test_fetch_table_logs_warning_for_key_first_seen_after_sample(monkeypatch, caplog):
    """WR-03 (LOW): a key that first appears in a record AFTER the bounded
    sample is silently dropped from every row -- must at least log a warning
    the first time this happens (previously no signal anywhere)."""
    monkeypatch.setattr(rest, "SAMPLE_SIZE", 1)

    class _PagedClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            page = int((params or {}).get("page", 1))
            data = {1: [{"id": 1}], 2: [{"id": 2, "extra": "x"}], 3: []}.get(page, [])
            return _FakeResp(data)

    monkeypatch.setattr(rest, "_client", lambda ds: _PagedClient())
    ds = DataSource(id="g3", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {
                            "path": "/users",
                            "pagination": {"type": "page", "param": "page", "max_pages": 10}}}}))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    with caplog.at_level(logging.WARNING):
        rows = list(st.rows)
    assert [r["id"] for r in rows] == [1, 2]
    assert "extra" not in rows[1]  # dropped, per D-17's documented tradeoff
    assert any("not present in the first" in rec.message for rec in caplog.records)


# --- 23-04 (QB-17, D-16): byte budget -> SyncTable.truncated -----------------


class _ByteResp:
    """A response double exposing a real `.content` (unlike the module's
    bare _FakeResp/_Resp-style doubles) so byte-budget tracking has actual
    wire bytes to count, mirroring a real httpx.Response's `.content`."""
    def __init__(self, payload, content_bytes):
        self._p = payload
        self.content = content_bytes

    def raise_for_status(self): pass
    def json(self): return self._p


def test_fetch_table_stream_stops_at_byte_budget_and_sets_truncated(monkeypatch):
    """Exceeding a (monkeypatched, small) MAX_SYNC_BYTES stops the stream
    early and yields a SyncTable with .truncated is True (D-16)."""
    monkeypatch.setattr(rest, "MAX_SYNC_BYTES", 10)

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            page = int((params or {}).get("page", 1))
            data = {1: [{"id": 1}], 2: [{"id": 2}]}.get(page, [])
            return _ByteResp(data, b"x" * 20)  # each page "costs" 20 bytes > the 10-byte budget

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="b1", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {
                            "path": "/users",
                            "pagination": {"type": "page", "param": "page", "max_pages": 5}}}}))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    rows = list(st.rows)
    assert rows == []  # the very first page already exceeds budget -> discarded
    assert st.truncated is True


def test_fetch_table_stream_under_budget_not_truncated(monkeypatch):
    """Under budget: .truncated is False, and every row is still yielded
    (the row cap (MAX_ROWS) bounds rows independently of the byte cap)."""
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            page = int((params or {}).get("page", 1))
            data = {1: [{"id": 1}], 2: []}.get(page, [])
            return _ByteResp(data, b"x" * 5)

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="b2", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {
                            "path": "/users",
                            "pagination": {"type": "page", "param": "page", "max_pages": 5}}}}))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    rows = list(st.rows)
    assert [r["id"] for r in rows] == [1]
    assert st.truncated is False


# --- CR-02 (HIGH, QB-17): non-paginated single-GET streams the byte budget --


class _StreamingHugeResp:
    """Simulates a real streamed httpx response: `iter_bytes()` yields
    chunks LAZILY and counts how many were actually pulled -- proving the
    streaming budget check aborts mid-body rather than materializing the
    whole (huge) response before checking MAX_SYNC_BYTES."""
    def __init__(self, num_chunks, chunk_size):
        self._num_chunks = num_chunks
        self._chunk_size = chunk_size
        self.chunks_yielded = 0

    def raise_for_status(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def iter_bytes(self):
        for _ in range(self._num_chunks):
            self.chunks_yielded += 1
            yield b"x" * self._chunk_size


def test_fetch_table_stream_single_get_truncates_without_loading_full_body(monkeypatch):
    """CR-02 (HIGH): a non-paginated REST response larger than
    MAX_SYNC_BYTES is truncated via client.stream(...) + iter_bytes()
    WITHOUT ever reading the whole (huge) body -- only a bounded number of
    chunks are pulled from the iterator before the budget check aborts the
    read (fail-before/pass-after: pre-fix code called client.get(), which
    would have pulled every one of the 1000 chunks via the fake's own
    full-buffering .content, before any budget check ran)."""
    monkeypatch.setattr(rest, "MAX_SYNC_BYTES", 100)  # tiny budget
    # 1000 chunks of 50 bytes each = 50,000 bytes total -- if the whole body
    # were read, chunks_yielded would end up at 1000. The 100-byte budget is
    # exceeded after just 3 chunks (150 bytes > 100).
    resp = _StreamingHugeResp(num_chunks=1000, chunk_size=50)

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, params=None, headers=None): return resp
        def get(self, url, params=None, headers=None):
            raise AssertionError("non-paginated branch must stream, not client.get()")

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="b3", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {"path": "/users"}}}))
    conn = rest.RestConnector(ds)
    st = conn.fetch_table("users")
    rows = list(st.rows)
    assert rows == []  # the oversized body is discarded, never partially yielded
    assert st.truncated is True
    # Only a bounded handful of chunks were pulled -- never the full 1000 --
    # proving the oversized body was never fully materialized in memory.
    assert resp.chunks_yielded < 10


def test_no_query_secret_error_message_unchanged(monkeypatch):
    """When auth.type != 'query' (or no auth configured), a REST error
    message keeps today's exact shape (still embeds the httpx str(e)) --
    the scrub only activates when a query secret is actually configured."""
    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            return httpx.Response(500, request=httpx.Request("GET", "https://x.test/users"))
        def stream(self, method, url, params=None, headers=None):
            return _CtxResp(httpx.Response(500, request=httpx.Request("GET", "https://x.test/users")))

    monkeypatch.setattr(rest, "_client", lambda ds: _Client())
    ds = DataSource(id="q6", name="api", source_type="rest",
                    options=json.dumps({
                        "base_url": "https://x.test",
                        "endpoints": {"users": {"path": "/users"}}}))
    conn = rest.RestConnector(ds)
    with pytest.raises(rest.ConnectorError, match="REST endpoint error:"):
        conn.fetch_table("users")
