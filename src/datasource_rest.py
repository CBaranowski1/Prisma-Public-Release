"""REST data-source connector. Each configured endpoint is a "table"; a GET
returns a JSON array (or an object with a configured records_path) that is
normalized into rows. httpx/httpcore are hard dependencies (requirements.txt),
imported at module scope so the connect-pinning classes below can subclass
them directly. v2: page + next-link pagination.
v3 (13-04, SEC-01/SEC-14): redirects are explicitly disabled and any 3xx/other
HTTPStatusError is translated into a clean ConnectorError; the once-resolved,
SSRF-validated IP is pinned for the actual TCP connect via a custom
httpcore.NetworkBackend, while Host header + TLS SNI keep deriving from the
original hostname.
v4 (23-02, QB-09): an opt-in `options.auth` block extends REST auth beyond
Bearer-only. Shape: `{"type": "bearer"|"api_key"|"header"|"basic"|"query",
"header": "<name>", "param": "<name>"}`. `bearer` (default/absent) and no
`options.auth` at all = today's exact behavior (`Authorization: Bearer
<password>` if `password` is set, else no auth header) -- purely additive,
opt-in, no regression. `api_key`/`header` (aliases) inject a configurable
header (`auth.header`, default `X-API-Key`) carrying `password`. `basic`
builds the httpx client with `auth=httpx.BasicAuth(username, password)`.
`query` merges a configurable query param (`auth.param`, default `api_key`)
carrying `password` into EVERY request's params -- test_connection and every
page in both `page` and `next` pagination modes -- without dropping existing
pagination params. Secrets always come from the encrypted `DataSource.
password`/`username` columns; `options.auth` holds only non-secret metadata
(type/header-name/param-name), never a secret value. A `query`-type secret's
REST error messages are built from `status_code`/`reason_phrase` only (never
`str(e)`/the URL) so the secret can't leak via httpx's URL-bearing exception
string.
v5 (23-04, QB-15/QB-17): `_fetch_records_stream` is a true generator -- it
yields each record as pages/next-links arrive instead of building one `out`
list, so `fetch_table(...).rows` streams instead of materializing the whole
source. Columns/types are inferred from a bounded first sample (~500 rows,
`SAMPLE_SIZE`) via `itertools.islice`, then the sample is `itertools.chain`-ed
back onto the remaining generator so no row is lost; keys that first appear
after the sample are dropped. In `page` pagination mode, each page's extracted
records are content-hashed (`hashlib.sha256` over a `sort_keys=True` JSON
dump); a page whose hash repeats a prior page's terminates pagination
immediately (a `page`-param-ignoring endpoint no longer loops `max_pages`
times accumulating duplicates) -- set `pagination.dedupe: false` on a
specific endpoint to disable this guard for a source with legitimately-
repeating pages. Response bytes (`resp.content`) are tracked across every
page/next-link/single-GET request against `MAX_SYNC_BYTES` (imported from
`datasource_connectors`); exceeding it -- or the existing `MAX_ROWS` row cap
-- stops the stream early and sets `SyncTable.truncated = True` (MD-01
reuse), so a bounded REST mirror is never mistaken for a complete sync."""
import hashlib
import itertools
import json
import logging
from urllib.parse import urlsplit, urljoin

import httpcore
import httpx
from httpx._config import create_ssl_context
from sqlalchemy.types import Integer, Float, Boolean, String

from src.datasource_connectors import (
    DataSourceConnector, SyncColumn, SyncTable, ConnectorError, MAX_SYNC_BYTES,
)
from src.ssrf_guard import assert_url_allowed, resolve_and_pin_host, SsrfBlocked

logger = logging.getLogger(__name__)

MAX_ROWS = 50000

# QB-17/D-17: bounded first-sample size used to infer columns/types before
# streaming the remainder -- a generator can't be scanned in full up-front
# (that would defeat the whole point of streaming), so column/type inference
# runs on this bounded sample only; keys that first appear after it are
# dropped (consistent with materialize_synctable keeping only known columns).
SAMPLE_SIZE = 500

# Hostnames for which pinning is skipped -- already-loopback base_urls have no
# rebind TOCTOU to close (the connection never leaves the local machine).
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _guard(url: str) -> None:
    """Reject a URL that resolves to a blocked (e.g. cloud-metadata) address,
    surfacing it as a ConnectorError so the route returns a clean 400."""
    try:
        assert_url_allowed(url)
    except SsrfBlocked as e:
        raise ConnectorError(f"This URL is not allowed: {e}")


def _host_only(netloc: str) -> str:
    """Strip userinfo and port from a URL netloc, mirroring
    ssrf_guard.assert_url_allowed's own extraction so pinning classifies the
    exact same bare hostname the guard checks."""
    host = netloc.split("@")[-1]
    if host.startswith("["):  # IPv6 literal [::1]:port
        host = host[1:].split("]")[0]
    else:
        host = host.split(":")[0]
    return host


class _PinnedBackend(httpcore.NetworkBackend):
    """Delegates to the real sync network backend, substituting the
    pre-validated pinned IP for the TCP connect target when `host` matches
    the guarded hostname (RESEARCH Pattern 2) -- closes the REST DNS-rebind
    TOCTOU window. Host header + TLS SNI are untouched: httpx/httpcore derive
    both from the origin's original hostname, never from what connect_tcp
    actually dialed.

    NOTE (Rule 1 correction to RESEARCH.md's sample code): `httpcore.
    NetworkBackend()` itself is an abstract base whose connect_tcp() raises
    NotImplementedError -- confirmed on-machine against the installed
    httpcore 1.0.9. The concrete default sync implementation is
    `httpcore.SyncBackend`, used here for delegation."""

    def __init__(self, guarded_host: str, pinned_ip: str):
        self._real = httpcore.SyncBackend()
        self._guarded_host = guarded_host
        self._pinned_ip = pinned_ip

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # SECURITY (CR-01): DNS is case-insensitive but this comparison isn't
        # unless both sides are normalized -- httpx/httpcore lowercase the
        # connect-target host before it reaches here, so a `_guarded_host`
        # stored with its original casing (e.g. from a mixed-case base_url)
        # would never match, silently falling through to `else host` and
        # reopening the DNS-rebind TOCTOU window the pin exists to close.
        target = self._pinned_ip if host.lower() == self._guarded_host else host
        return self._real.connect_tcp(
            target, port, timeout=timeout, local_address=local_address,
            socket_options=socket_options)


class PinnedHTTPTransport(httpx.HTTPTransport):
    """An httpx.HTTPTransport whose TCP connect is pinned to a pre-resolved,
    SSRF-validated IP for one guarded hostname. Deliberately does NOT call
    httpx.HTTPTransport.__init__ -- handle_request/handle_async_request only
    read self._pool (verified against installed httpx 0.28.1/httpcore 1.0.9)."""

    def __init__(self, guarded_host: str, pinned_ip: str, verify=True):
        ssl_context = create_ssl_context(verify=verify)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context,
            network_backend=_PinnedBackend(guarded_host, pinned_ip),
        )


def _dig(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _extract_records(payload, records_path):
    if records_path:
        payload = _dig(payload, records_path) if isinstance(payload, dict) else None
    if not isinstance(payload, list):
        raise ConnectorError("REST endpoint did not return a JSON array")
    return [r for r in payload if isinstance(r, dict)]


def _response_byte_len(resp) -> int:
    """Actual wire bytes for one response (D-16) -- `.content` on a real
    httpx.Response is the raw body already read by client.get(), so no
    re-serialization is needed. Falls back to a JSON re-encode ONLY for this
    module's own bare test doubles (a `raise_for_status()`/`json()` pair with
    no `.content`, e.g. `_FakeResp`/`_Resp`-style fixtures across the test
    suite) -- never hit by a real httpx.Response in production."""
    content = getattr(resp, "content", None)
    if content is not None:
        return len(content)
    try:
        return len(json.dumps(resp.json()).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _translate_status_error(e: httpx.HTTPStatusError, *, _scrub_query_secret: bool) -> ConnectorError:
    """Shared non-2xx -> ConnectorError translation (RESEARCH Pattern 4 /
    Pitfall 2), used by BOTH the plain buffered-GET path (`_get_checked`) and
    the streaming single-GET path (CR-02 below) -- identical redirect /
    query-secret-scrub behavior either way, so extracting the streaming path
    couldn't silently drop the Pitfall-3 scrub.

    `_scrub_query_secret` (23-02, QB-09/Pitfall 3): when a `query`-type auth
    secret is configured, `httpx.HTTPStatusError.__str__()` embeds the FULL
    request URL including the secret-carrying query string -- so the
    generic-error branch below must build its message from
    `status_code`/`reason_phrase` only, never `str(e)`/`e.response.url`, or
    the secret leaks into the ConnectorError / route 400 body / logs."""
    if e.response is not None and e.response.is_redirect:
        return ConnectorError(
            "endpoint returned a redirect; configure the source with the final URL"
        )
    if _scrub_query_secret and e.response is not None:
        return ConnectorError(
            f"REST endpoint error: {e.response.status_code} {e.response.reason_phrase}"
        )
    return ConnectorError(f"REST endpoint error: {e}")


def _get_checked(client, url, *, _scrub_query_secret=False, **kw):
    """GET `url` and raise_for_status(), translating any non-2xx into a clean
    ConnectorError instead of letting httpx.HTTPStatusError escape uncaught
    (RESEARCH Pattern 4 / Pitfall 2). A 3xx (redirect) gets a specific,
    actionable message; any other status gets a generic wrapped message.
    Non-status httpx errors (ConnectError, TimeoutException, ...) are NOT
    caught here -- narrowing those is deferred to Phase 14's SEC-05
    broad-except burn-down (RESEARCH Open Question 1).

    `_scrub_query_secret` is keyword-only and consumed here, never forwarded
    to `client.get(**kw)`."""
    resp = client.get(url, **kw)
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise _translate_status_error(e, _scrub_query_secret=_scrub_query_secret) from e
    return resp


def _query_auth_active(ds) -> bool:
    """True when this connector's configured auth is `query`-type AND a
    secret is actually present -- gates both the params-merge and the
    Pitfall-3 error-message scrub."""
    auth_cfg = getattr(ds, "_rest_auth", None) or {}
    return auth_cfg.get("type") == "query" and bool(ds.password)


def _merged_query_auth_params(ds, base):
    """Merge the query-auth secret param into `base` (never dropping
    existing pagination params), returning `base` UNCHANGED when no query
    secret is configured -- preserves the exact no-params call shape older
    call sites/test doubles rely on (D-07/D-08)."""
    if not _query_auth_active(ds):
        return base
    auth_cfg = getattr(ds, "_rest_auth", None) or {}
    param_name = auth_cfg.get("param") or "api_key"
    merged = dict(base or {})
    merged[param_name] = ds.password
    return merged


def _client(ds):
    # httpx is a hard dependency (requirements.txt) imported at module scope
    # above; if it were ever missing, `import src.datasource_rest` itself
    # would already have failed loudly before reaching this function.
    # D-05/D-07 (23-02, QB-09): `_rest_auth` is stashed on `ds` by
    # RestConnector.__init__ (mirroring `_rest_pinned_host_ip`'s stash, so
    # `_client(ds)` keeps its single-arg signature for existing test
    # monkeypatches). Absent/unknown type -> today's exact Bearer-or-nothing
    # behavior (D-07 back-compat). `query`-type auth carries no header here;
    # it is merged into the request `params` at each call site instead
    # (Task 2, D-08).
    auth_cfg = getattr(ds, "_rest_auth", None) or {}
    auth_type = auth_cfg.get("type") or "bearer"
    headers = {}
    kwargs = {
        "timeout": 30.0,
        # SECURITY (SSRF): disable redirects explicitly (don't rely on
        # httpx's own version-dependent default) -- a redirect to an
        # internal/metadata host must never be followed carrying the Bearer
        # token (mirrors webhook_manager.py's idiom).
        "follow_redirects": False,
    }
    if auth_type == "basic":
        # WR-01 (MEDIUM): DataSource.username is nullable and nothing
        # upstream validates it's set when auth.type == "basic" --
        # httpx.BasicAuth(None, ...) raises a raw TypeError, which the
        # /test, /tables, and /upload routes' connectivity checks don't
        # catch (only ConnectorError is caught there), surfacing as an
        # unhandled 500 instead of a clean 400. Raise explicitly instead.
        if not ds.username:
            raise ConnectorError("auth.type 'basic' requires a username to be configured")
        # httpx-native Basic auth -- no manual base64 (D-05).
        kwargs["auth"] = httpx.BasicAuth(ds.username, ds.password)
    elif auth_type in ("api_key", "header"):
        header_name = auth_cfg.get("header") or "X-API-Key"
        if ds.password:
            headers[header_name] = ds.password
    elif auth_type != "query":
        # "bearer" or any unrecognized type falls back to today's behavior.
        if ds.password:
            headers["Authorization"] = f"Bearer {ds.password}"
    kwargs["headers"] = headers
    # D-03: thread the once-resolved pinned IP (stashed by RestConnector
    # .__init__ on the ds instance, since `_client(ds)` must keep its single-
    # arg signature for existing test monkeypatches) into a pinning transport.
    pin = getattr(ds, "_rest_pinned_host_ip", None)
    if pin is not None:
        guarded_host, pinned_ip = pin
        kwargs["transport"] = PinnedHTTPTransport(guarded_host, pinned_ip)
    return httpx.Client(**kwargs)


def _infer_type(values):
    seen = [v for v in values if v is not None]
    if seen and all(isinstance(v, bool) for v in seen):
        return Boolean()
    if seen and all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return Integer()
    if seen and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return Float()
    return String()


class RestConnector(DataSourceConnector):
    def __init__(self, ds):
        self._ds = ds
        try:
            self._opts = json.loads(ds.options or "{}")
        except (json.JSONDecodeError, TypeError):
            self._opts = {}
        self._base = (self._opts.get("base_url") or "").rstrip("/")
        self._endpoints = self._opts.get("endpoints") or {}
        # D-05/D-06 (23-02, QB-09): auth is opt-in metadata only (type,
        # header name, query-param name) -- the secret itself always comes
        # from ds.password/ds.username (EncryptedText), never from options.
        # Same dict-tolerant "or {}" parsing as _base/_endpoints above; no
        # new try/except needed, the outer json.loads already guards.
        self._auth = self._opts.get("auth") or {}
        # Stashed on `ds` (not passed as a `_client()` parameter) for the
        # same reason `_rest_pinned_host_ip` is stashed below: `_client(ds)`
        # must keep its single-argument signature for existing test
        # monkeypatches (`monkeypatch.setattr(rest, "_client", ...)`).
        self._ds._rest_auth = self._auth
        # D-03: resolve + SSRF-validate the base_url host ONCE per connector
        # construction and pin it for every subsequent request this
        # connector's lifetime makes (test_connection/list_tables/
        # fetch_table each call _client() independently) -- never re-resolve
        # per call, or the DNS-rebind TOCTOU window reopens (RESEARCH Pattern
        # 1's lifetime pitfall, mirrored here for REST). Loopback base_urls
        # are skipped -- there is no rebind risk connecting to the local
        # machine, and pinning "127.0.0.1" to itself is a no-op.
        # SECURITY (CR-01): lowercase here so the stored guarded host matches
        # what httpx/httpcore hand to _PinnedBackend.connect_tcp, which
        # always sees a lowercased connect-target host (DNS is case-
        # insensitive, so normalizing is safe).
        host = _host_only(urlsplit(self._base).netloc).lower() if self._base else ""
        if host and host not in _LOOPBACK_HOSTS:
            try:
                pinned_ip = resolve_and_pin_host(host)
            except SsrfBlocked as e:
                raise ConnectorError(f"This URL is not allowed: {e}") from e
            # Stashed on `ds` (not passed as a `_client()` parameter) so
            # `_client(ds)`'s single-argument signature stays stable for
            # existing test monkeypatches (`monkeypatch.setattr(rest,
            # "_client", lambda ds: ...)` in tests/test_datasource_rest*.py).
            self._ds._rest_pinned_host_ip = (host, pinned_ip)

    def make_engine(self):
        raise ConnectorError("REST sources have no SQL engine")

    def reflect_table(self, name: str):
        raise ConnectorError("REST sources do not support reflection")

    def test_connection(self) -> None:
        _guard(self._base or "/")
        with _client(self._ds) as c:
            params = _merged_query_auth_params(self._ds, None)
            scrub = _query_auth_active(self._ds)
            if params:
                _get_checked(c, self._base or "/", params=params, _scrub_query_secret=scrub)
            else:
                _get_checked(c, self._base or "/", _scrub_query_secret=scrub)

    def list_tables(self):
        return list(self._endpoints.keys())

    def _fetch_records_stream(self, cfg, st: SyncTable):
        """True generator (QB-17/D-15): yields each record as pages/next-links
        arrive in page mode, next mode, and the single-GET else branch --
        never accumulating a whole-source `out` list. Preserves `_guard(url)`,
        the Plan-02 (23-02) query-param auth merge at every `_get_checked`
        call, and the next-mode off-host guard verbatim.

        `st` (D-16) is the SAME SyncTable instance `fetch_table` returns to
        its caller -- mutated in place (`st.truncated = True`) whenever
        either budget cap fires, so the flag reflects reality once the
        caller has drained the generator, regardless of how much of it has
        been consumed so far (a dataclass instance is a mutable object the
        generator closure and the caller both hold a reference to)."""
        url = self._base + cfg.get("path", "")
        _guard(url)  # block metadata/link-local before any request
        records_path = cfg.get("records_path")
        pg = cfg.get("pagination") or {}
        # D-08 (23-02, QB-09): the query-auth secret must ride on the SAME
        # pinned, `_guard`-checked, follow_redirects=False request as every
        # other REST call -- merged into `params` at each call site below,
        # never replacing the pagination params already there.
        scrub = _query_auth_active(self._ds)
        rows_yielded = 0
        bytes_read = 0

        def _over_byte_budget(resp) -> bool:
            # D-16: accumulate ACTUAL wire bytes (resp.content) across every
            # page/next-link/single-GET request -- no re-serialization. Stop
            # fetching further pages (discarding the page that pushed the
            # cumulative total over budget) once MAX_SYNC_BYTES is reached.
            nonlocal bytes_read
            bytes_read += _response_byte_len(resp)
            if bytes_read >= MAX_SYNC_BYTES:
                st.truncated = True
                return True
            return False

        with _client(self._ds) as c:
            if pg.get("type") == "page":
                param = pg.get("param", "page")
                size_param = pg.get("size_param")
                size = pg.get("size")
                page = int(pg.get("start", 1))
                # QB-15/D-14: hash each page's normalized record content and
                # terminate on the FIRST repeat -- a page-param-ignoring
                # endpoint returning the same non-empty page every call would
                # otherwise loop max_pages times accumulating duplicates.
                # Opt-out (Pitfall 5): pagination.dedupe: false disables ONLY
                # this guard; max_pages/MAX_ROWS/empty-page backstops remain.
                dedupe = pg.get("dedupe", True)
                seen_hashes = set()
                for _ in range(int(pg.get("max_pages", 50))):
                    params = {param: page}
                    if size_param and size is not None:
                        params[size_param] = size
                    params = _merged_query_auth_params(self._ds, params)
                    resp = _get_checked(c, url, params=params, _scrub_query_secret=scrub)
                    if _over_byte_budget(resp):
                        return
                    recs = _extract_records(resp.json(), records_path)
                    if not recs:
                        break
                    if dedupe:
                        page_hash = hashlib.sha256(
                            json.dumps(recs, sort_keys=True, default=str).encode()
                        ).hexdigest()
                        if page_hash in seen_hashes:
                            logger.warning(
                                "REST page %s repeated prior page content for source %s; "
                                "endpoint may be ignoring the page param -- terminating "
                                "pagination", page, getattr(self._ds, "name", "?"))
                            return
                        seen_hashes.add(page_hash)
                    for r in recs:
                        yield r
                        rows_yielded += 1
                        if rows_yielded >= MAX_ROWS:
                            st.truncated = True
                            return
                    page += 1
                else:
                    # CR-01/MD-01: the loop ran to full max_pages exhaustion
                    # without an internal `break`/`return` (the `if not recs:
                    # break` "genuinely done" signal, the dedupe-repeat
                    # `return`, and the MAX_ROWS `return` above all skip this
                    # `else` -- for...else only fires on true range exhaustion).
                    # That means there may be MORE pages than max_pages allowed
                    # for -- this is a bounded, not a complete, mirror.
                    logger.warning(
                        "REST page pagination for source %s hit max_pages=%s "
                        "without the source signalling completion; the mirror "
                        "may be truncated", getattr(self._ds, "name", "?"),
                        pg.get("max_pages", 50))
                    st.truncated = True
            elif pg.get("type") == "next":
                next_field = pg.get("next_field", "next")
                base_host = urlsplit(self._base).netloc.lower()
                next_url = url
                for _ in range(int(pg.get("max_pages", 50))):
                    params = _merged_query_auth_params(self._ds, None)
                    if params:
                        resp = _get_checked(c, next_url, params=params, _scrub_query_secret=scrub)
                    else:
                        resp = _get_checked(c, next_url, _scrub_query_secret=scrub)
                    if _over_byte_budget(resp):
                        return
                    body = resp.json()
                    recs = _extract_records(body, records_path)
                    for r in recs:
                        yield r
                        rows_yielded += 1
                        if rows_yielded >= MAX_ROWS:
                            st.truncated = True
                            return
                    nxt = _dig(body, next_field) if isinstance(body, dict) else None
                    if not nxt:
                        break
                    # SECURITY (SSRF): the next URL comes from the external
                    # response, so an attacker-controlled/compromised upstream
                    # could point it at internal or cloud-metadata addresses.
                    # Only follow a next page on the SAME host as the configured
                    # base_url (relative URLs resolve to that host); anything
                    # off-host ends pagination.
                    resolved = urljoin(self._base + "/", str(nxt))
                    if urlsplit(resolved).netloc.lower() != base_host:
                        logger.warning(
                            "REST next-link host mismatch for source %s; not following %r",
                            getattr(self._ds, "name", "?"), nxt)
                        break
                    next_url = resolved
                else:
                    # CR-01/MD-01: same range-exhaustion signal as the `page`
                    # branch above -- the loop ran out of max_pages iterations
                    # without the source signalling "no next link" (the `if
                    # not nxt: break`) or an off-host next link (the host-
                    # mismatch `break`) or a MAX_ROWS/byte-budget `return`.
                    logger.warning(
                        "REST next-link pagination for source %s hit "
                        "max_pages=%s without the source signalling "
                        "completion; the mirror may be truncated",
                        getattr(self._ds, "name", "?"), pg.get("max_pages", 50))
                    st.truncated = True
            else:
                # CR-02 (HIGH, QB-17): this single-GET branch is the MOST
                # exposed to a single oversized response -- there is no
                # per-page size bound the way pagination naturally provides.
                # A real httpx.Client.get() always fully buffers the body
                # BEFORE _over_byte_budget ever runs, giving MAX_SYNC_BYTES
                # zero protection against one huge body (e.g. an
                # attacker-influenced/compromised upstream per this file's
                # own SSRF threat model). client.stream(...) + iter_bytes()
                # lets the budget check fire WHILE reading, so an oversized
                # body is never fully materialized in process memory.
                params = _merged_query_auth_params(self._ds, None)
                get_kwargs = {"params": params} if params else {}
                with c.stream("GET", url, **get_kwargs) as resp:
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        raise _translate_status_error(e, _scrub_query_secret=scrub) from e
                    chunks = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > MAX_SYNC_BYTES:
                            # Stop reading BEFORE the rest of an oversized
                            # body is buffered -- discard what's been read so
                            # far, matching the paginated branches' "discard
                            # the page that pushed over budget" behavior.
                            st.truncated = True
                            return
                        chunks.append(chunk)
                    body = json.loads(b"".join(chunks))
                recs = _extract_records(body, records_path)
                if len(recs) > MAX_ROWS:
                    st.truncated = True
                for r in recs[:MAX_ROWS]:
                    yield r

    def fetch_table(self, name: str) -> SyncTable:
        cfg = self._endpoints.get(name)
        if not cfg:
            raise ConnectorError(f"Unknown REST endpoint: {name!r}")
        st = SyncTable(columns=[], rows=iter(()))
        gen = self._fetch_records_stream(cfg, st)
        # D-17: infer columns/types from a BOUNDED first sample only -- a
        # generator can't be scanned in full without defeating QB-17's whole
        # point. itertools.islice peeks SAMPLE_SIZE rows without losing them;
        # itertools.chain below re-joins the sample onto the remaining
        # generator so every row is still yielded exactly once.
        sample = list(itertools.islice(gen, SAMPLE_SIZE))
        keys = []
        for r in sample:
            for k in r:
                if k not in keys:
                    keys.append(k)
        st.columns = [SyncColumn(k, _infer_type([r.get(k) for r in sample])) for k in keys]

        def _cell(v):
            return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v

        def _rows():
            # WR-03 (LOW): a key first appearing in a record AFTER the bounded
            # sample is silently dropped below (only `keys` -- resolved from
            # the sample -- survive into each row dict). That's an
            # intentional streaming tradeoff, but it was previously
            # unobservable: no log/signal anywhere. Warn once per newly-seen
            # dropped key so this is at least visible in logs.
            warned_keys = set()
            for r in itertools.chain(sample, gen):
                extra = r.keys() - keys - warned_keys
                if extra:
                    logger.warning(
                        "REST endpoint %r for source %s returned column(s) %s "
                        "not present in the first %d sampled rows -- these "
                        "values are dropped from every row (bounded-sample "
                        "tradeoff, D-17)",
                        name, getattr(self._ds, "name", "?"), sorted(extra), SAMPLE_SIZE)
                    warned_keys |= extra
                yield {k: _cell(r.get(k)) for k in keys}

        st.rows = _rows()
        return st
