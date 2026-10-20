"""RED->GREEN suite for SEC-25 (structural loopback trust) + the shared
`client_identity` core it co-locates with (SEC-18 keying, plan 17-02).

Exercises the NEW/rewritten helpers in `core/middleware.py` directly at the
unit level: `is_trusted_loopback` (rewritten from an enumerated proxy-header
denylist to a structural allowlist-by-absence check) and `client_identity`
(new shared `TRUST_PROXY`-aware derivation core, D-02/D-04).

Uses a REAL `starlette.datastructures.Headers` instance (not a mock) so the
case-insensitive multidict lookup behavior is genuinely exercised, per
17-RESEARCH.md's verified Starlette API fact (Headers lowercases every
header name at construction time -- no manual `.lower()` needed anywhere).
"""
import pytest
from starlette.datastructures import Headers

import core.middleware as mw

LOOPBACK_HOSTS = ["127.0.0.1", "::1"]

# One member per structurally-distinct forwarding/client-IP-disclosure header
# shape (17-RESEARCH.md Pattern 1 / 17-CONTEXT.md D-01). Enumerated here only
# to prove EACH shape is individually caught -- the held-out cases below are
# what actually proves detection is structural, not a renamed denylist.
FORWARDING_HEADERS = [
    "true-client-ip",
    "x-client-ip",
    "fastly-client-ip",
    "x-cluster-client-ip",
    "fly-client-ip",
    "cf-connecting-ip",
    "cf-ray",
    "x-forwarded-for",
    "forwarded",
    "via",
    "x-real-ip",
]

# Header names NEVER enumerated anywhere in 17-CONTEXT.md/17-RESEARCH.md's
# explicit lists -- proves the predicate is SHAPE-based (structural), not an
# enumerated list under a different name.
HELD_OUT_HEADERS = [
    "x-foo-real-ip",
    "x-azure-client-ip",
]


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Minimal stand-in exposing exactly what is_trusted_loopback/
    client_identity read: `.client.host` and a real Starlette `Headers`."""

    def __init__(self, host, headers=None):
        self.client = _FakeClient(host) if host is not None else None
        self.headers = Headers(headers or {})


@pytest.mark.parametrize("host", LOOPBACK_HOSTS)
def test_direct_loopback_no_headers_is_trusted(host):
    """Pitfall 6 non-regression: a bare direct-loopback request with ZERO
    forwarding headers must still be trusted -- the structural rewrite must
    not over-tighten and break local admin/setup."""
    req = _FakeRequest(host)
    assert mw.is_trusted_loopback(req) is True


@pytest.mark.parametrize("host", LOOPBACK_HOSTS)
@pytest.mark.parametrize("header_name", FORWARDING_HEADERS)
def test_loopback_with_forwarding_header_is_not_trusted(host, header_name):
    req = _FakeRequest(host, headers={header_name: "1.2.3.4"})
    assert mw.is_trusted_loopback(req) is False


@pytest.mark.parametrize("host", LOOPBACK_HOSTS)
@pytest.mark.parametrize("header_name", HELD_OUT_HEADERS)
def test_loopback_with_held_out_header_is_not_trusted(host, header_name):
    """A header name never enumerated in 17-CONTEXT.md still trips detection
    because the check is structural (shape), not a renamed denylist."""
    req = _FakeRequest(host, headers={header_name: "1.2.3.4"})
    assert mw.is_trusted_loopback(req) is False


def test_non_loopback_peer_no_headers_is_not_trusted():
    req = _FakeRequest("203.0.113.7")
    assert mw.is_trusted_loopback(req) is False


def test_client_identity_trust_proxy_unset_returns_client_host(monkeypatch):
    monkeypatch.setattr(mw, "TRUST_PROXY", "")
    req = _FakeRequest("203.0.113.7", headers={"x-forwarded-for": "9.9.9.9"})
    assert mw.client_identity(req) == "203.0.113.7"


def test_client_identity_trust_proxy_set_returns_header_leftmost_value(monkeypatch):
    monkeypatch.setattr(mw, "TRUST_PROXY", "x-forwarded-for")
    req = _FakeRequest("127.0.0.1", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
    assert mw.client_identity(req) == "9.9.9.9"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "203.0.113.7"])
def test_shared_core_agrees_when_trust_proxy_unset(monkeypatch, host):
    """D-02 cross-check: with TRUST_PROXY unset, both client_identity and
    is_trusted_loopback derive their notion of 'the client' from
    request.client.host -- they can never disagree."""
    monkeypatch.setattr(mw, "TRUST_PROXY", "")
    req = _FakeRequest(host)
    identity = mw.client_identity(req)
    assert identity == host
    assert mw.is_trusted_loopback(req) == (identity in ("127.0.0.1", "::1"))


def test_shared_core_agrees_when_trust_proxy_set(monkeypatch):
    """D-02 cross-check: with TRUST_PROXY set, both helpers derive from the
    SAME named header -- a request with a non-loopback direct peer but a
    TRUST_PROXY-named header claiming 127.0.0.1 is judged loopback by BOTH
    helpers on the derived client, never the raw peer."""
    monkeypatch.setattr(mw, "TRUST_PROXY", "x-forwarded-for")
    req = _FakeRequest("203.0.113.7", headers={"x-forwarded-for": "127.0.0.1"})
    identity = mw.client_identity(req)
    assert identity == "127.0.0.1"
    assert mw.is_trusted_loopback(req) is True
