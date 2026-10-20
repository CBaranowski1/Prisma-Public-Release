"""SC-5 (D-13): prove the whole Phase-15 auth hardening on the ASSEMBLED
FastAPI app via `starlette.testclient.TestClient`, mirroring
`tests/test_verify_qb_query_access_integration.py`'s role in Phase 14
("presence != integration").

Two required halves plus a full-surface functioning drive:

  (1) Full-surface drive: login, POST /2fa/backup-codes/regenerate, the
      admin reset-other-user endpoint, and /switch-user are all driven
      through ONE real, cookie-authenticated TestClient session against the
      real, composed app -- a genuine `/login` (no auth shim), not a faked
      `validate_token`/`get_username_for_token` bypass -- proving these
      SEC-19/SEC-20/SEC-21 surfaces function together end-to-end on the
      assembled app, not merely in isolated unit tests.

  (2) SC-5a (D-13a, T-15-17): concurrent config mutations driven from real
      `threading.Thread` workers through the auth routes (POST
      /api/auth/users), not by calling AuthManager directly, must lose no
      update -- proven against the real, composed app's own live
      `auth_manager` instance and its on-disk auth.json.

  (3) SC-5b (D-13b, T-15-21a): the passwordless dev-mint path
      (`PRISMA_DEV_SWITCHER`) is inert on a network-exposed bind, proven via
      a real (non-shimmed) admin session driving /switch-user with a
      spoofed `X-Forwarded-For` header -- the exact shape a Cloudflare
      tunnel / reverse proxy presents -- through the assembled app.

Reuses the `_heal_polluted_modules` / `app_module_instance` assembled-app
harness from `tests/test_verify_qb_privilege.py:114-215` VERBATIM (14-PATTERNS
precedent, confirmed again in 15-PATTERNS.md) -- do NOT build a new harness.

Never imports `core.auth` / `routes.auth_routes` handlers directly for any
assembled-app assertion in this file -- every auth-lifecycle surface is
driven through `TestClient` against `app_module_instance.app`. The ONE
exception is `app_module_instance._assert_dev_mint_allowed`, which is the
assembled app's OWN module-level startup-gate function (not a route
handler) -- calling it directly on the reimported `app` module is itself
"driving the assembled app", per 15-RESEARCH.md Code Example 4 and
`tests/test_dev_mint_hard_gate.py`'s own precedent; this file does not
re-prove that half again (already covered there) beyond the /switch-user
route-level proof below.

Each auth-mutating test redirects the real, shared `auth_manager` instance's
`auth_path` to a `tmp_path` scratch file and resets its in-memory `_config`
to an empty dict via `monkeypatch` (auto-reverted at test teardown) -- this
NEVER touches the real `data/auth.json` on the host running this suite.
Mutating the instance's ATTRIBUTES (not replacing the object) is required
because `routes/auth_routes.py`'s `setup_auth_routes(auth_manager)` closure
captured this exact instance at app.py's own import time (app.py:181) --
rebinding the module-level `auth_manager` name to a new object would leave
that closure pointing at the stale original (see `tests/test_dev_mint_hard_gate.py`'s
`_bypass_auth_as_admin` docstring for the same trap, itself citing STATE.md's
Phase 9 P5 note).
"""
import importlib.util
import json
import os
import sys
import threading
from unittest.mock import MagicMock

import pyotp
import pytest
from starlette.testclient import TestClient


# ---------------------------------------------------------------------------
# Assembled-app harness (verbatim idiom from tests/test_verify_qb_privilege.py
# L114-166 -- kept inline per that file's own precedent: importing a fixture
# across test modules is not reliable).
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
    """Build the assembled app with AUTH_ENABLED forced on, so the real
    AuthMiddleware is on the request path for every test in this file
    (otherwise every gate this file proves would be a no-op)."""
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    # Environment gap (DEBT-03, out of Phase-15 scope): bs4 is a documented
    # dependency (requirements.txt) but not installed in this venv. Stub it
    # ONLY for the duration of building the assembled app, ONLY if genuinely
    # absent, then remove the stub immediately after.
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


def _seed_scratch_auth(monkeypatch, real_auth, tmp_path):
    """Redirect the REAL, shared auth_manager instance's persistence to a
    tmp_path scratch file and reset its in-memory config, WITHOUT replacing
    the object (see module docstring on the closure trap). monkeypatch
    reverts both attributes at test teardown, so this never leaks into the
    real data/auth.json and never leaks between tests in this module."""
    monkeypatch.setattr(real_auth, "auth_path", str(tmp_path / "auth.json"))
    monkeypatch.setattr(real_auth, "_config", {"users": {}})
    return real_auth


def _login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("ok") is True
    return resp


# ---------------------------------------------------------------------------
# (1) Full-surface drive: login -> admin reset-password -> 2FA backup-codes
# regenerate -> /switch-user, all through ONE real, cookie-authenticated
# TestClient session -- no auth shim anywhere in this test.
# ---------------------------------------------------------------------------

def test_full_auth_lifecycle_surfaces_via_assembled_app(app_module_instance, monkeypatch, tmp_path):
    real_auth = _seed_scratch_auth(monkeypatch, app_module_instance.auth_manager, tmp_path)
    assert real_auth.create_user("admin", "AdminPW123!", is_admin=True)
    assert real_auth.create_user("bob", "BobPW123!", is_admin=False)

    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(app_module_instance.app)

    # --- Real /login (SEC-21 D-12 non-regression: password-first, no shim) ---
    _login(client, "admin", "AdminPW123!")
    assert client.cookies.get(SESSION_COOKIE)

    # --- Admin reset-other-user (SEC-20 D-06) ---
    resp = client.post("/api/auth/users/bob/reset-password", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["must_change_password"] is True
    assert real_auth.users["bob"]["must_change_password"] is True

    # --- 2FA backup-codes regenerate (SEC-19 D-04), real TOTP enable first ---
    secret = real_auth.totp_generate_secret("admin")
    assert secret
    code = pyotp.TOTP(secret).now()
    initial_codes = real_auth.totp_confirm_enable("admin", code)
    assert initial_codes is not None
    old_hashes = list(real_auth.users["admin"]["totp_backup_code_hashes"])

    resp = client.post("/api/auth/2fa/backup-codes/regenerate", json={"password": "AdminPW123!"})
    assert resp.status_code == 200, resp.text
    new_codes = resp.json()["backup_codes"]
    assert len(new_codes) == 10
    new_hashes = real_auth.users["admin"]["totp_backup_code_hashes"]
    assert new_hashes != old_hashes, "regeneration must invalidate the old backup-code set"
    assert all(c not in new_hashes for c in new_codes), "plaintext codes must never equal a stored hash"

    # --- /switch-user (SEC-21 D-11), genuinely loopback + admin + switcher on ---
    monkeypatch.setenv("PRISMA_DEV_SWITCHER", "1")
    token = client.cookies.get(SESSION_COOKIE)
    loopback_client = TestClient(
        app_module_instance.app,
        client=("127.0.0.1", 54321),
        cookies={SESSION_COOKIE: token},
    )
    resp = loopback_client.post("/api/auth/switch-user", json={"username": "bob"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "bob"


# ---------------------------------------------------------------------------
# (2) SC-5a (D-13a): concurrent config writes driven from real threads
# THROUGH the auth routes -- no lost update, no torn on-disk snapshot.
# ---------------------------------------------------------------------------

def test_concurrent_admin_create_user_no_lost_update_via_testclient(app_module_instance, monkeypatch, tmp_path):
    """~40 real threading.Thread workers each POST a distinct new user to
    /api/auth/users on the assembled app, authenticated as the same admin
    session, against the REAL shared auth_manager instance. Every created
    user must be present in memory AND in a valid, parseable on-disk
    auth.json -- proving _config_lock (SEC-17) serializes correctly under
    genuine thread contention driven through the full request stack, not
    just a direct AuthManager call (that half is already proven by
    tests/test_auth_config_lock_concurrency.py; this is the assembled-app
    integration half, D-13a)."""
    real_auth = _seed_scratch_auth(monkeypatch, app_module_instance.auth_manager, tmp_path)
    assert real_auth.create_user("admin", "AdminPW123!", is_admin=True)

    from routes.auth_routes import SESSION_COOKIE

    login_client = TestClient(app_module_instance.app)
    _login(login_client, "admin", "AdminPW123!")
    token = login_client.cookies.get(SESSION_COOKIE)
    assert token

    client = TestClient(app_module_instance.app, cookies={SESSION_COOKIE: token})

    names = [f"cuser{i}" for i in range(40)]
    errors = []
    errors_lock = threading.Lock()

    def _create(name):
        try:
            resp = client.post(
                "/api/auth/users",
                json={"username": name, "password": "pw12345678", "is_admin": False},
            )
            if resp.status_code != 200:
                with errors_lock:
                    errors.append((name, resp.status_code, resp.text))
        except Exception as e:  # noqa: BLE001 - thread exceptions must surface to the test
            with errors_lock:
                errors.append((name, "exception", str(e)))

    threads = [threading.Thread(target=_create, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], f"Unexpected failures from concurrent create-user calls: {errors}"

    # No lost updates in memory.
    assert set(names).issubset(real_auth.users.keys())

    # No torn/partial on-disk snapshot: the file must be valid, parseable
    # JSON reflecting every committed user, proving _save() never wrote a
    # torn write even under concurrent request-driven contention.
    on_disk = json.loads((tmp_path / "auth.json").read_text())
    assert set(names).issubset(on_disk["users"].keys())
    assert "admin" in on_disk["users"]


# ---------------------------------------------------------------------------
# (3) SC-5b (D-13b): dev-mint inert on a network-exposed bind -- proven via
# a genuine (non-shimmed) admin session and a spoofed proxy header, through
# the assembled app's real /switch-user route.
# ---------------------------------------------------------------------------

def test_dev_mint_inert_on_network_exposed_bind_via_switch_user(app_module_instance, monkeypatch, tmp_path):
    """The passwordless dev-mint path must stay inert on a network-exposed
    bind even for a genuinely, freshly-authenticated admin session (no auth
    shim) -- the client.host the ASGI transport reports is a genuinely
    loopback value (exactly what a Cloudflare-tunneled / reverse-proxied
    caller looks like from the app's perspective), but an X-Forwarded-For
    header is present. A bare `client.host in (...)` check would incorrectly
    ALLOW this request (the anti-pattern 15-RESEARCH.md calls out); only the
    real `is_trusted_loopback` proxy-header rejection correctly refuses it."""
    real_auth = _seed_scratch_auth(monkeypatch, app_module_instance.auth_manager, tmp_path)
    assert real_auth.create_user("admin", "AdminPW123!", is_admin=True)
    assert real_auth.create_user("bob", "BobPW123!", is_admin=False)

    from routes.auth_routes import SESSION_COOKIE

    login_client = TestClient(app_module_instance.app)
    _login(login_client, "admin", "AdminPW123!")
    token = login_client.cookies.get(SESSION_COOKIE)
    assert token

    monkeypatch.setenv("PRISMA_DEV_SWITCHER", "1")
    spoofed_client = TestClient(
        app_module_instance.app,
        client=("127.0.0.1", 54322),
        cookies={SESSION_COOKIE: token},
    )
    resp = spoofed_client.post(
        "/api/auth/switch-user",
        json={"username": "bob"},
        headers={"X-Forwarded-For": "1.2.3.4"},
    )
    assert resp.status_code == 403, resp.text

    # No side effect from the refused attempt: bob never receives a minted
    # session, and admin's own session/config state is unaffected.
    assert real_auth.users["bob"].get("must_change_password") is not True
    assert real_auth.users["admin"]["is_admin"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
