# tests/test_datasource_rest_pagination.py
"""REST pagination: page mode + next-link mode aggregate across pages; a
non-paginated endpoint still does a single GET. httpx is monkeypatched."""
import json
import socket

import pytest

import src.datasource_rest as rest
from core.querybuilder_models import DataSource


@pytest.fixture(autouse=True)
def _mock_dns(monkeypatch):
    # "x.test" is an IANA-reserved never-resolving TLD (RFC 6761) used as a
    # fixture host here; the ssrf_guard (13-02, D-02) now fails CLOSED on an
    # unresolvable host, so these pagination-logic tests (which don't exercise
    # DNS) need a deterministic mocked resolution to a public IP.
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                 ("93.184.216.34", 0))])


class _Resp:
    def __init__(self, payload): self._p = payload
    def raise_for_status(self): pass
    def json(self): return self._p
    # CR-02 (HIGH, QB-17): also usable as its own `client.stream(...)`
    # context manager for the non-paginated single-GET branch below.
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def iter_bytes(self):
        yield json.dumps(self._p).encode("utf-8")


class _PageClient:
    """Returns 2 pages of users then an empty page (page mode)."""
    def __init__(self): self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {})))
        page = int((params or {}).get("page", 1))
        data = {1: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}.get(page, [])
        return _Resp(data)


class _NextClient:
    """Returns page1 with a next link, then page2 with no next (next mode)."""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None, headers=None):
        if url.endswith("/p2"):
            return _Resp({"items": [{"id": 3}], "next": None})
        return _Resp({"items": [{"id": 1}, {"id": 2}], "next": "https://x.test/p2"})


def _ds(endpoints):
    return DataSource(id="s1", name="api", source_type="rest",
                      options=json.dumps({"base_url": "https://x.test", "endpoints": endpoints}))


def test_page_mode_aggregates_until_empty(monkeypatch):
    client = _PageClient()
    monkeypatch.setattr(rest, "_client", lambda ds: client)
    ds = _ds({"users": {"path": "/users",
                        "pagination": {"type": "page", "param": "page", "start": 1,
                                       "size_param": "per_page", "size": 2, "max_pages": 10}}})
    st = rest.RestConnector(ds).fetch_table("users")
    assert [r["id"] for r in st.rows] == [1, 2, 3]
    assert client.calls[0][1].get("per_page") == 2  # size param sent


def test_next_link_mode_follows_next(monkeypatch):
    monkeypatch.setattr(rest, "_client", lambda ds: _NextClient())
    ds = _ds({"users": {"path": "/users", "records_path": "items",
                        "pagination": {"type": "next", "next_field": "next", "max_pages": 10}}})
    st = rest.RestConnector(ds).fetch_table("users")
    assert [r["id"] for r in st.rows] == [1, 2, 3]


def test_no_pagination_single_get(monkeypatch):
    class _One:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None): return _Resp([{"id": 1}])
        # CR-02 (HIGH, QB-17): the non-paginated branch now reads via
        # client.stream(...) + iter_bytes() rather than client.get().
        def stream(self, method, url, params=None, headers=None): return _Resp([{"id": 1}])
    monkeypatch.setattr(rest, "_client", lambda ds: _One())
    ds = _ds({"users": {"path": "/users"}})
    st = rest.RestConnector(ds).fetch_table("users")
    assert [r["id"] for r in st.rows] == [1]


class _SsrfNextClient:
    """First page returns a `next` pointing at a DIFFERENT host (metadata IP) —
    an SSRF attempt the connector must NOT follow."""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None, headers=None):
        if "169.254" in url or "evil.test" in url:
            raise AssertionError(f"must not fetch off-host next URL: {url}")
        return _Resp({"items": [{"id": 1}], "next": "http://169.254.169.254/latest/meta-data/"})


def test_next_link_off_host_is_not_followed(monkeypatch):
    monkeypatch.setattr(rest, "_client", lambda ds: _SsrfNextClient())
    ds = _ds({"users": {"path": "/users", "records_path": "items",
                        "pagination": {"type": "next", "next_field": "next", "max_pages": 10}}})
    st = rest.RestConnector(ds).fetch_table("users")
    # Off-host next (cloud-metadata IP) ignored — only the first page is returned.
    assert [r["id"] for r in st.rows] == [1]


class _RelativeNextClient:
    """next given as a same-host relative path must still be followed."""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None, headers=None):
        if url.endswith("/p2"):
            return _Resp({"items": [{"id": 2}], "next": None})
        return _Resp({"items": [{"id": 1}], "next": "/p2"})


def test_next_link_relative_same_host_is_followed(monkeypatch):
    monkeypatch.setattr(rest, "_client", lambda ds: _RelativeNextClient())
    ds = _ds({"users": {"path": "/users", "records_path": "items",
                        "pagination": {"type": "next", "next_field": "next", "max_pages": 10}}})
    st = rest.RestConnector(ds).fetch_table("users")
    assert [r["id"] for r in st.rows] == [1, 2]


# --- 23-04 (QB-15, D-14): page-hash dedup terminates a page-ignoring endpoint ---


class _PageIgnoringClient:
    """Returns the SAME non-empty page content regardless of the requested
    `page` param -- simulates an endpoint that ignores pagination entirely."""
    def __init__(self): self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None, headers=None):
        self.calls.append(dict(params or {}))
        return _Resp([{"id": 1}, {"id": 2}])


def test_page_ignoring_endpoint_terminates_on_first_repeat(monkeypatch):
    """A page-param-ignoring endpoint terminates on the FIRST repeated page
    hash -- total rows == one page's worth, NOT max_pages x one page (QB-15).
    A termination warning is logged."""
    client = _PageIgnoringClient()
    monkeypatch.setattr(rest, "_client", lambda ds: client)
    ds = _ds({"users": {"path": "/users",
                        "pagination": {"type": "page", "param": "page", "max_pages": 50}}})
    st = rest.RestConnector(ds).fetch_table("users")
    assert [r["id"] for r in st.rows] == [1, 2]
    # page 1 fetched + yielded; page 2's identical content is detected as a
    # repeat and pagination stops -- never all 50 max_pages.
    assert len(client.calls) == 2


def test_page_mode_normal_multi_page_unaffected_by_dedupe(monkeypatch):
    """A normal endpoint with genuinely distinct pages still aggregates every
    page -- the dedup guard must not false-terminate distinct content."""
    client = _PageClient()
    monkeypatch.setattr(rest, "_client", lambda ds: client)
    ds = _ds({"users": {"path": "/users",
                        "pagination": {"type": "page", "param": "page", "max_pages": 10}}})
    st = rest.RestConnector(ds).fetch_table("users")
    assert [r["id"] for r in st.rows] == [1, 2, 3]


def test_page_mode_dedupe_opt_out_allows_legit_repeat(monkeypatch):
    """`pagination.dedupe: false` disables ONLY the hash guard -- the
    max_pages/MAX_ROWS/empty-page backstops still bound the loop (Pitfall 5)."""
    client = _PageIgnoringClient()
    monkeypatch.setattr(rest, "_client", lambda ds: client)
    ds = _ds({"users": {"path": "/users",
                        "pagination": {"type": "page", "param": "page", "max_pages": 3,
                                       "dedupe": False}}})
    st = rest.RestConnector(ds).fetch_table("users")
    rows = list(st.rows)
    # dedup disabled: every one of the 3 max_pages iterations returns the
    # identical 2-row page, so all 3 pages' worth are aggregated (6 rows).
    assert len(rows) == 6
    assert len(client.calls) == 3
    # CR-01/MD-01: more pages existed than max_pages allowed for (dedupe was
    # disabled specifically so distinct-looking pages could keep coming) --
    # this is a bounded mirror, never a complete one.
    assert st.truncated is True


# --- CR-01 (BLOCKER, MD-01): max_pages backstop must signal truncation ------


class _EndlessDistinctPageClient:
    """Every page has genuinely distinct (non-repeating) content -- there is
    always one more page available than any max_pages cap could exhaust."""
    def __init__(self): self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None, headers=None):
        page = int((params or {}).get("page", 1))
        self.calls.append(page)
        return _Resp([{"id": page}])


def test_page_mode_max_pages_backstop_sets_truncated(monkeypatch):
    """A paginating endpoint with more pages than `max_pages` (the loop runs
    to full range-exhaustion, never hitting the empty-page/dedupe/MAX_ROWS
    exits) must yield `truncated == True` -- a bounded mirror must never be
    mistaken for a complete sync (CR-01/MD-01)."""
    client = _EndlessDistinctPageClient()
    monkeypatch.setattr(rest, "_client", lambda ds: client)
    ds = _ds({"users": {"path": "/users",
                        "pagination": {"type": "page", "param": "page", "max_pages": 3}}})
    st = rest.RestConnector(ds).fetch_table("users")
    rows = list(st.rows)
    assert len(rows) == 3
    assert len(client.calls) == 3
    assert st.truncated is True


class _EndlessNextClient:
    """Every page has a same-host `next` link -- pagination never signals
    "done" on its own; only max_pages bounds it."""
    def __init__(self): self.calls = 0
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None, headers=None):
        self.calls += 1
        n = self.calls
        return _Resp({"items": [{"id": n}], "next": "https://x.test/p2"})


def test_next_mode_max_pages_backstop_sets_truncated(monkeypatch):
    """Same MD-01 signal for `next`-link pagination: exhausting max_pages
    without the source ever returning a falsy `next` must set truncated."""
    client = _EndlessNextClient()
    monkeypatch.setattr(rest, "_client", lambda ds: client)
    ds = _ds({"users": {"path": "/users", "records_path": "items",
                        "pagination": {"type": "next", "next_field": "next", "max_pages": 3}}})
    st = rest.RestConnector(ds).fetch_table("users")
    rows = list(st.rows)
    assert len(rows) == 3
    assert client.calls == 3
    assert st.truncated is True
