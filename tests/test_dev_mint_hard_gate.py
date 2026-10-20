"""SEC-21 D-10/D-11: prove the passwordless dev-session mint cannot enable on
a network-exposed run.

Two distinct enforcement points, tested separately (15-RESEARCH.md Pattern 4
-- they need different mechanisms because only one of them ever has a
Request object to inspect):

  1. Startup gate (D-10): `app.py`'s dev-switcher block must fail LOUD
     (RuntimeError) unless `_PRISM_RESOLVED_BIND_HOST` (written only by
     server_main.py, BEFORE it imports app) confirms a loopback bind.
     Absence of the signal (Docker CMD / bare uvicorn / start-macos.sh, all
     of which bypass server_main.py) must be treated as "unknown, assume
     exposed" -- fail closed, never fail open. Driven directly against the
     extracted gate helper (`app._assert_dev_mint_allowed`) so this half
     doesn't need a full app reload per case.

  2. `/switch-user` call-time gate (D-11): the always-correct backstop that
     inspects the REAL request via `is_trusted_loopback` -- proven through
     the assembled app via TestClient, not a bare `client.host` check, so a
     spoofed X-Forwarded-For header from a "loopback" peer is rejected too
     (this is the exact shape a Cloudflare tunnel / reverse proxy presents).
"""
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared assembled-app fixture (reused verbatim from
# tests/test_verify_qb_privilege.py:114-215 -- see that file for the full
# rationale on each step; kept inline here per that file's own note that
# importing a fixture across test modules is not reliable).
# ---------------------------------------------------------------------------

def _heal_polluted_modules():
    """Sibling unit tests stub model modules (src.request_models, core.models)
    with MagicMock and import route modules against them, leaving routes.*
    bound to mocks in sys.modules. That makes app route-registration raise
    FastAPIError when we build the full app. Drop mock-contaminated model
    modules, then force app + all routes.* to re-import so they rebind
    against the real models."""
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def app_module_instance():
    """Build the assembled app with AUTH_ENABLED forced on. PRISMA_DEV_SWITCHER
    is deliberately left as whatever the ambient environment has it (never set
    by this fixture) -- the dev-mint startup gate this file proves is exercised
    directly via the extracted helper function, not by re-triggering app
    import under PRISMA_DEV_SWITCHER=1 (which would make this fixture's own
    import order-sensitive to that env var)."""
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    # Environment gap (DEBT-03, out of scope here): bs4 is a documented
    # dependency but not installed in this venv. Stub it ONLY for the
    # duration of building the assembled app, and ONLY if genuinely absent.
    _stubbed_bs4 = False
    if "bs4" not in sys.modules and importlib.util.find_spec("bs4") is None:
        sys.modules["bs4"] = MagicMock()
        _stubbed_bs4 = True
    saved_auth_enabled = os.environ.get("AUTH_ENABLED")
    os.environ["AUTH_ENABLED"] = "true"
    try:
        import app as app_module
    finally:
        if saved_auth_enabled is None:
            os.environ.pop("AUTH_ENABLED", None)
        else:
            os.environ["AUTH_ENABLED"] = saved_auth_enabled
        if _stubbed_bs4:
            sys.modules.pop("bs4", None)
    return app_module


# ---------------------------------------------------------------------------
# D-10: startup gate -- absent/non-loopback fail-loud, loopback proceeds.
# ---------------------------------------------------------------------------

def test_absent_resolved_bind_host_fails_loud(app_module_instance):
    """Docker CMD / bare uvicorn / start-macos.sh all bypass server_main.py
    and never set _PRISM_RESOLVED_BIND_HOST. Absence must be treated as
    unknown -> assume exposed -> fail closed, NEVER as a falsely-safe default
    (RESEARCH Pitfall 9 -- the load-bearing finding of this phase)."""
    with pytest.raises(RuntimeError, match=r"(?i)refusing to seed dev accounts"):
        app_module_instance._assert_dev_mint_allowed(None)


def test_non_loopback_resolved_bind_host_fails_loud(app_module_instance):
    """A confirmed non-loopback bind (e.g. the Dockerfile's `--host 0.0.0.0`)
    must fail exactly as loud as an absent signal -- never silently seed."""
    with pytest.raises(RuntimeError, match=r"(?i)refusing to seed dev accounts"):
        app_module_instance._assert_dev_mint_allowed("0.0.0.0")


def test_loopback_resolved_bind_host_does_not_raise(app_module_instance):
    """A genuinely confirmed loopback bind (server_main.py's own default,
    `--host 127.0.0.1`) is the ONLY case that may proceed to seeding."""
    app_module_instance._assert_dev_mint_allowed("127.0.0.1")  # must not raise


def test_ipv6_and_localhost_loopback_forms_do_not_raise(app_module_instance):
    """The loopback allowlist covers the other equivalent loopback spellings
    a `--host` value could realistically take."""
    app_module_instance._assert_dev_mint_allowed("::1")
    app_module_instance._assert_dev_mint_allowed("localhost")


# ---------------------------------------------------------------------------
# D-11: /switch-user call-time re-check via the assembled app (TestClient).
# ---------------------------------------------------------------------------

def _bypass_auth_as_admin(app_module_instance, monkeypatch):
    """Instance-level patch (established pattern, see STATE.md Phase 9 P5):
    mutate the REAL shared auth_manager instance's methods rather than
    replacing the object. routes/auth_routes.py's `setup_auth_routes(auth_manager)`
    closure captured this exact instance at app.py import time (app.py:562)
    -- rebinding the module-level `auth_manager` attribute to a NEW fake
    object would leave that closure pointing at the stale original, and the
    route would never observe our admin-authenticated shim."""
    real_auth = app_module_instance.auth_manager
    monkeypatch.setattr(type(real_auth), "is_configured", property(lambda self: True))
    # `users` is a read-only property (no setter) backed by `self._config` --
    # AuthMiddleware only ever does `auth_manager.users.get(user, {})`, which
    # is safe against a real config lacking an "admin" key (falls through to
    # `{}`, so `must_change_password` reads as falsy). No patch needed here.
    monkeypatch.setattr(real_auth, "validate_token", lambda token: True)
    monkeypatch.setattr(real_auth, "get_username_for_token", lambda token: "admin")
    monkeypatch.setattr(real_auth, "is_admin", lambda u: True)
    return real_auth


def test_switch_user_denies_spoofed_forwarded_for_despite_loopback_client_host(app_module_instance, monkeypatch):
    """The client.host the ASGI transport reports is 127.0.0.1 -- exactly
    what a Cloudflare-tunneled / reverse-proxied caller looks like from the
    app's perspective -- but an X-Forwarded-For header is present. This
    proves `is_trusted_loopback`'s proxy-header rejection specifically; a
    bare `client.host in ('127.0.0.1', '::1')` check would incorrectly
    ALLOW this request (anti-pattern explicitly called out in 15-RESEARCH.md)."""
    _bypass_auth_as_admin(app_module_instance, monkeypatch)
    monkeypatch.setenv("PRISMA_DEV_SWITCHER", "1")
    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(
        app_module_instance.app,
        client=("127.0.0.1", 12345),
        cookies={SESSION_COOKIE: "faketoken"},
    )
    resp = client.post(
        "/api/auth/switch-user",
        json={"username": "coder"},
        headers={"X-Forwarded-For": "1.2.3.4"},
    )
    assert resp.status_code == 403


def test_switch_user_denies_genuinely_non_loopback_client(app_module_instance, monkeypatch):
    """A genuinely remote client.host (no proxy header needed to prove the
    point) must also be rejected."""
    _bypass_auth_as_admin(app_module_instance, monkeypatch)
    monkeypatch.setenv("PRISMA_DEV_SWITCHER", "1")
    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(
        app_module_instance.app,
        client=("8.8.8.8", 12345),
        cookies={SESSION_COOKIE: "faketoken"},
    )
    resp = client.post("/api/auth/switch-user", json={"username": "coder"})
    assert resp.status_code == 403


def test_switch_user_disabled_entirely_without_prisma_dev_switcher(app_module_instance, monkeypatch):
    """Non-regression: with the switcher env var unset/false, the pre-existing
    'Switcher disabled' 403 still fires first, regardless of client host."""
    _bypass_auth_as_admin(app_module_instance, monkeypatch)
    monkeypatch.delenv("PRISMA_DEV_SWITCHER", raising=False)
    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(
        app_module_instance.app,
        client=("127.0.0.1", 12345),
        cookies={SESSION_COOKIE: "faketoken"},
    )
    resp = client.post("/api/auth/switch-user", json={"username": "coder"})
    assert resp.status_code == 403
