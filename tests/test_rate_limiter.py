"""Tests for the RateLimiter — pure in-memory, no server needed.

Also covers SEC-18 (D-04, phase 17-02): the auth rate limiters must key on
`core.middleware.client_identity(request)` — the shared TRUST_PROXY-aware
derivation core landed in plan 17-01 — instead of raw `request.client.host`,
so a proxy/tunnel front-end doesn't collapse every remote client into one
shared bucket.
"""
import importlib.util
import sys
import time
from unittest.mock import MagicMock

import pytest
from starlette.datastructures import Headers
from starlette.testclient import TestClient

from src.rate_limiter import RateLimiter
import core.middleware as mw


class TestRateLimiterAllow:
    def test_allows_under_limit(self):
        rl = RateLimiter(max_requests=3, window_seconds=60)
        assert rl.check("ip1") is True
        assert rl.check("ip1") is True
        assert rl.check("ip1") is True

    def test_blocks_over_limit(self):
        rl = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            rl.check("ip1")
        assert rl.check("ip1") is False

    def test_different_keys_independent(self):
        rl = RateLimiter(max_requests=1, window_seconds=60)
        assert rl.check("ip1") is True
        assert rl.check("ip2") is True
        assert rl.check("ip1") is False
        assert rl.check("ip2") is False


class TestRateLimiterExpiry:
    def test_window_expiry(self):
        rl = RateLimiter(max_requests=1, window_seconds=1)
        assert rl.check("ip1") is True
        assert rl.check("ip1") is False
        time.sleep(1.1)
        assert rl.check("ip1") is True


class TestRateLimiterCleanup:
    def test_cleanup_removes_stale_entries(self):
        rl = RateLimiter(max_requests=1, window_seconds=1)
        rl._cleanup_interval = 0  # Force cleanup on every check
        rl.check("ip1")
        assert "ip1" in rl._log
        time.sleep(1.1)
        rl.check("ip2")  # Triggers cleanup
        assert "ip1" not in rl._log


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    """Minimal stand-in exposing exactly what client_identity reads:
    `.client.host` and a real Starlette `Headers` — mirrors
    tests/test_trust_boundary_core.py's fake-request approach."""

    def __init__(self, host, headers=None):
        self.client = _FakeClient(host) if host is not None else None
        self.headers = Headers(headers or {})


class TestClientIdentityKeying:
    """SEC-18 (D-04): unit-level proof that client_identity(request) behaves
    correctly on both TRUST_PROXY branches before it's wired into the auth
    rate limiters (Task 2 of this plan)."""

    def test_trust_proxy_unset_returns_client_host_verbatim(self, monkeypatch):
        monkeypatch.setattr(mw, "TRUST_PROXY", "")
        req = _FakeRequest("203.0.113.9", headers={"x-forwarded-for": "9.9.9.9"})
        assert mw.client_identity(req) == "203.0.113.9"

    def test_trust_proxy_set_returns_derived_header_value(self, monkeypatch):
        monkeypatch.setattr(mw, "TRUST_PROXY", "x-forwarded-for")
        req = _FakeRequest("127.0.0.1", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
        assert mw.client_identity(req) == "9.9.9.9"

    def test_rate_limiter_keyed_on_client_identity_gives_independent_budgets(self, monkeypatch):
        """RateLimiter-level proof: two requests sharing the SAME peer but
        DIFFERENT TRUST_PROXY-named header values, keyed on
        client_identity(), get independent budgets — the bucket-separation
        property auth_routes.py's three limiters must exhibit once re-keyed
        (Task 2)."""
        monkeypatch.setattr(mw, "TRUST_PROXY", "x-forwarded-for")
        rl = RateLimiter(max_requests=1, window_seconds=60)
        req_a = _FakeRequest("127.0.0.1", headers={"x-forwarded-for": "9.9.9.9"})
        req_b = _FakeRequest("127.0.0.1", headers={"x-forwarded-for": "8.8.8.8"})
        assert rl.check(mw.client_identity(req_a)) is True
        assert rl.check(mw.client_identity(req_b)) is True
        # Each identity's SECOND request is throttled independently — a
        # shared-bucket (raw-host) keying would have already exhausted the
        # budget for req_b once req_a's first call consumed it.
        assert rl.check(mw.client_identity(req_a)) is False
        assert rl.check(mw.client_identity(req_b)) is False


def _heal_polluted_modules():
    """Sibling unit tests stub model modules (src.request_models, core.models)
    with MagicMock and import route modules against them, leaving routes.*
    bound to mocks in sys.modules. That makes app route-registration raise
    FastAPIError when we build the full app. Drop mock-contaminated model
    modules, then force app + all routes.* to re-import so they rebind
    against the real models. (Reused verbatim from
    tests/test_dev_mint_hard_gate.py.)"""
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def app_module_instance():
    """Build the assembled app once for this module (pattern reused from
    tests/test_dev_mint_hard_gate.py)."""
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    _stubbed_bs4 = False
    if "bs4" not in sys.modules and importlib.util.find_spec("bs4") is None:
        sys.modules["bs4"] = MagicMock()
        _stubbed_bs4 = True
    try:
        import app as app_module
    finally:
        if _stubbed_bs4:
            sys.modules.pop("bs4", None)
    return app_module


class TestAuthLimiterKeyingIntegration:
    """SEC-18 load-bearing proof: the auth_routes.py limiters THEMSELVES key
    on client_identity(request), not raw request.client.host. This is the
    only assertion in this plan that exercises routes/auth_routes.py's actual
    keying, and it fails under the pre-Task-2 `request.client.host` keying
    (RED): two distinct forwarded identities sharing one TestClient peer
    would collapse into ONE shared bucket under raw-host keying, so identity
    B's request would incorrectly come back 429 once identity A's budget is
    exhausted."""

    def test_distinct_forwarded_identities_get_independent_signup_budgets(self, app_module_instance, monkeypatch):
        app_module = app_module_instance
        real_auth = app_module.auth_manager
        # Force deterministic, side-effect-free signup handling regardless of
        # the real on-disk auth.json state — is_configured=False makes every
        # non-throttled request hit the same "Run setup first" 400 branch, so
        # no real user account is ever written by this test.
        monkeypatch.setattr(type(real_auth), "is_configured", property(lambda self: False))
        monkeypatch.setattr(mw, "TRUST_PROXY", "x-forwarded-for")
        client = TestClient(app_module.app)

        # Exhaust identity A's 3-request/300s signup budget.
        for _ in range(3):
            resp = client.post(
                "/api/auth/signup",
                json={"username": "identity-a-user", "password": "longenoughpassword"},
                headers={"X-Forwarded-For": "9.9.9.9"},
            )
            assert resp.status_code != 429

        resp_a_throttled = client.post(
            "/api/auth/signup",
            json={"username": "identity-a-user", "password": "longenoughpassword"},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )
        assert resp_a_throttled.status_code == 429

        # Identity B shares the SAME underlying TestClient peer but a
        # DIFFERENT forwarded identity — it must still have its own,
        # untouched budget. This is exactly what raw-host keying could not
        # provide (load-bearing proof).
        resp_b = client.post(
            "/api/auth/signup",
            json={"username": "identity-b-user", "password": "longenoughpassword"},
            headers={"X-Forwarded-For": "8.8.8.8"},
        )
        assert resp_b.status_code != 429
