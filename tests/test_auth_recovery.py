"""SEC-20 proof: admin reset-other-user, offline CLI break-glass,
`must_change_password` forced-rotation gating, and the deliberate D-08
absence of any web self-service password-reset endpoint.

Four independent proof groups:
  1. Manager-level (`AuthManager.admin_reset_password` / `cli_reset_password` /
     `change_password` flag-clear) -- mirrors tests/test_auth_session_revocation.py's
     `_make_manager` harness.
  2. CLI break-glass subprocess smoke (`python -m core.auth reset-password <user>`)
     against a scratch `PRISM_DATA_DIR`, never the real data dir.
  3. Route-level admin gating (`POST /users/{username}/reset-password`) + the
     D-08 absence assertion (exactly one reset-shaped route, admin-gated, not
     auth-exempt).
  4. Assembled-app `must_change_password` middleware gate (D-09) -- mirrors
     tests/test_verify_qb_privilege.py's TestClient harness.

RED until Task 2 (manager methods + CLI) and Task 3 (route + middleware gate) land.
"""

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Shared module-isolation helpers (mirrors tests/test_auth_session_revocation.py)
#
# DEBT-03 (33-08): this file previously forced a "real" core.auth via a raw
# sys.modules.pop("core.auth", None) + delattr(core, "auth") + reimport dance
# (Pitfall 1), then permanently overwrote auth_mod._hash_password /
# _verify_password with raw attribute assignment (Pitfall 2) -- neither
# mutation was ever reverted, so every pytest process that collected this
# file afterward ran with a permanently fake, unsalted hashing scheme bound
# into core.auth. Verified (33-08 bisection) this is the direct cause of the
# same test_tool_run_query.py / test_verify_qb_spine_integration.py pollution
# 33-03 fixed in the sibling test_auth_config_lock_concurrency.py -- a
# separate file with the identical bug shape. Verified empirically that no
# other test file stubs core/core.auth/routes.auth_routes in sys.modules, so
# the forcing dance was unneeded defensive legacy code here too (same as
# Assumption A3 in 33-RESEARCH.md) -- fixed the same way: plain imports of
# the real, already-loaded modules, with any fake-hashing swap applied via
# monkeypatch.setattr so pytest auto-reverts it at each test's teardown.
# ---------------------------------------------------------------------------

def _make_manager(tmp_path, monkeypatch):
    """Admin + one regular user, fast fake hashing (mirrors the
    session-revocation harness). Uses monkeypatch.setattr on the real,
    already-imported core.auth module so the fake hashing is auto-reverted
    at teardown -- no raw sys.modules mutation, no permanent hash leak."""
    import core.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")
    monkeypatch.setattr(auth_mod, "_verify_password", lambda password, hashed: hashed == f"hash:{password}")
    auth_path = tmp_path / "auth.json"
    mgr = auth_mod.AuthManager(str(auth_path))
    assert mgr.create_user("admin", "admin-pw", is_admin=True)
    assert mgr.create_user("bob", "bob-pw", is_admin=False)
    return mgr


# ---------------------------------------------------------------------------
# Group 1: manager-level admin_reset_password / cli_reset_password /
# change_password flag-clear
# ---------------------------------------------------------------------------

def test_admin_reset_password_admin_resets_bob_and_revokes_sessions(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)
    old_hash = mgr.users["bob"]["password_hash"]
    session = mgr.create_session("bob", "bob-pw")
    assert mgr.validate_token(session) is True

    ok = mgr.admin_reset_password("bob", "new-temp-pw", requesting_user="admin")

    assert ok is True
    assert mgr.users["bob"]["password_hash"] != old_hash
    assert mgr.users["bob"]["must_change_password"] is True
    assert mgr.validate_token(session) is False
    assert mgr.verify_password("bob", "new-temp-pw") is True


def test_admin_reset_password_non_admin_caller_refused_no_mutation(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)
    old_hash = mgr.users["bob"]["password_hash"]

    ok = mgr.admin_reset_password("bob", "new-temp-pw", requesting_user="bob")

    assert ok is False
    assert mgr.users["bob"]["password_hash"] == old_hash
    assert mgr.users["bob"].get("must_change_password") is not True


def test_cli_reset_password_resets_with_no_admin_argument(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)
    old_hash = mgr.users["bob"]["password_hash"]
    session = mgr.create_session("bob", "bob-pw")

    ok = mgr.cli_reset_password("bob", "cli-temp-pw")

    assert ok is True
    assert mgr.users["bob"]["password_hash"] != old_hash
    assert mgr.users["bob"]["must_change_password"] is True
    assert mgr.validate_token(session) is False
    assert mgr.verify_password("bob", "cli-temp-pw") is True


def test_cli_reset_password_unknown_user_returns_false(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)

    assert mgr.cli_reset_password("ghost-user", "whatever") is False


def test_change_password_clears_must_change_password_flag(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)
    assert mgr.admin_reset_password("bob", "temp-pw-1", requesting_user="admin") is True
    assert mgr.users["bob"]["must_change_password"] is True

    ok = mgr.change_password("bob", "temp-pw-1", "final-pw-2")

    assert ok is True
    assert mgr.users["bob"]["must_change_password"] is False


# ---------------------------------------------------------------------------
# Group 2: offline CLI break-glass subprocess smoke (D-07)
# ---------------------------------------------------------------------------

def test_cli_entrypoint_resets_password_via_subprocess(tmp_path, monkeypatch):
    """`python -m core.auth reset-password <user>` against a scratch
    PRISM_DATA_DIR -- never the real data dir."""
    data_dir = tmp_path / "cli_scratch"
    data_dir.mkdir()
    auth_path = data_dir / "auth.json"

    import core.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")
    seed_mgr = auth_mod.AuthManager(str(auth_path))
    assert seed_mgr.create_user("alice", "alice-original-pw", is_admin=True)

    env = dict(os.environ)
    env["PRISM_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [sys.executable, "-m", "core.auth", "reset-password", "alice"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "must_change_password" in result.stdout.lower() or "must change" in result.stdout.lower()

    with open(auth_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["users"]["alice"]["must_change_password"] is True
    assert data["users"]["alice"]["password_hash"] != "hash:alice-original-pw"


def test_cli_entrypoint_unknown_user_nonzero_exit(tmp_path):
    data_dir = tmp_path / "cli_scratch_unknown"
    data_dir.mkdir()

    env = dict(os.environ)
    env["PRISM_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [sys.executable, "-m", "core.auth", "reset-password", "ghost-user"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert result.stderr.strip() != ""


# ---------------------------------------------------------------------------
# Group 3: route-level admin gating + D-08 absence
# ---------------------------------------------------------------------------

def _build_router(auth):
    from routes.auth_routes import setup_auth_routes
    return setup_auth_routes(auth)


def _reset_password_endpoint(auth):
    from routes.auth_routes import setup_auth_routes, AdminResetPasswordRequest
    router = setup_auth_routes(auth)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/users/{username}/reset-password":
            return route.endpoint, AdminResetPasswordRequest
    raise AssertionError("admin reset-password route not found")


def test_reset_password_route_denies_non_admin_no_mutation():
    auth = MagicMock()
    auth.get_username_for_token.return_value = "bob"
    auth.is_admin.return_value = False
    endpoint, AdminResetPasswordRequest = _reset_password_endpoint(auth)
    request = SimpleNamespace(cookies={"prisma_session": "tok"})
    body = AdminResetPasswordRequest(password=None)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(username="bob", body=body, request=request))

    assert exc.value.status_code == 403
    auth.admin_reset_password.assert_not_called()


def test_reset_password_route_allows_admin_and_delegates_to_manager():
    auth = MagicMock()
    auth.get_username_for_token.return_value = "admin"
    auth.is_admin.return_value = True
    auth.admin_reset_password.return_value = True
    endpoint, AdminResetPasswordRequest = _reset_password_endpoint(auth)
    request = SimpleNamespace(cookies={"prisma_session": "tok"})
    body = AdminResetPasswordRequest(password="explicit-temp-pw")

    result = asyncio.run(endpoint(username="bob", body=body, request=request))

    assert result["ok"] is True
    assert result["must_change_password"] is True
    auth.admin_reset_password.assert_called_once_with("bob", "explicit-temp-pw", requesting_user="admin")


_RESET_WORD_RE = re.compile(r"\breset\b")


def test_d08_exactly_one_reset_route_and_it_is_admin_gated():
    """D-08: no web self-service reset endpoint. Only the admin-gated
    reset-other-user route may exist. Uses a word-boundary match so an
    unrelated path substring (e.g. "presets") never false-positives."""
    router = _build_router(MagicMock())
    reset_routes = [
        r for r in router.routes
        if _RESET_WORD_RE.search((getattr(r, "path", "") or "").lower())
    ]

    assert len(reset_routes) == 1, f"expected exactly one reset-related route, found: {[r.path for r in reset_routes]}"
    route = reset_routes[0]
    assert route.path == "/api/auth/users/{username}/reset-password"
    assert "POST" in (route.methods or set())


def test_d08_reset_password_not_in_auth_exempt_allowlist():
    """The admin-gated reset endpoint must never be reachable without
    authentication -- confirm it's absent from app.py's auth-exempt set.
    Word-boundary match so "presets" (a genuinely exempt path) never
    false-positives against "reset"."""
    app_text = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    match = re.search(r"AUTH_EXEMPT_EXACT\s*=\s*\{(.*?)\}", app_text, re.S)
    assert match, "AUTH_EXEMPT_EXACT set literal not found in app.py"
    assert not _RESET_WORD_RE.search(match.group(1).lower())


# ---------------------------------------------------------------------------
# Pitfall 7: cli_reset_password must never be reachable from an HTTP path
# ---------------------------------------------------------------------------

def test_cli_reset_password_call_token_absent_from_routes_and_tool_paths():
    """The break-glass call token must appear ONLY in core/auth.py's own
    __main__ block (and this test file's assertions above) -- never in
    routes/*.py or src/tool_implementations.py. Token built from parts so
    this assertion's own source text can't accidentally self-match the
    files being scanned (which are production files, not this test)."""
    token = "cli_reset" + "_password("

    hits = []
    routes_dir = REPO_ROOT / "routes"
    for py_file in sorted(routes_dir.glob("*.py")):
        text = py_file.read_text(encoding="utf-8")
        if token in text:
            hits.append(str(py_file))

    tool_impl = REPO_ROOT / "src" / "tool_implementations.py"
    if tool_impl.exists():
        text = tool_impl.read_text(encoding="utf-8")
        if token in text:
            hits.append(str(tool_impl))

    assert hits == [], f"'{token}' call token found outside its allowed scope: {hits}"


# ---------------------------------------------------------------------------
# Group 4: assembled-app must_change_password middleware gate (D-09)
# mirrors tests/test_verify_qb_privilege.py's TestClient harness.
# ---------------------------------------------------------------------------

def _heal_polluted_modules():
    """Sibling unit tests stub model modules with MagicMock and import route
    modules against them, leaving routes.*/app bound to mocks in
    sys.modules. Force a clean re-import of app + all routes.* so the
    assembled app is built against real objects (mirrors
    tests/test_verify_qb_privilege.py:114-130). core.auth is deliberately
    NOT evicted here (33-08): this file's own hash-function fakes are now
    applied exclusively via monkeypatch.setattr (see _make_manager), which
    auto-reverts at each test's teardown -- by the time this module-scoped
    fixture runs, core.auth is already the untouched real module, so
    force-evicting it would only reintroduce the Pitfall-1 module-identity
    divergence this phase is closing."""
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def app_module_instance():
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    # DEBT-03 environment gap: bs4 is a documented dependency but not
    # installed in this venv; app.py's import chain hard-imports it. Stub
    # ONLY for the duration of building the assembled app, only if genuinely
    # absent (mirrors tests/test_verify_qb_privilege.py:143-153).
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


def _fake_gate_auth_manager(*, username, flagged):
    """Test-only auth shim confined to this file: a stand-in object whose
    `.users` dict carries the must_change_password flag the new middleware
    gate reads. Not a modification of shipped source."""
    return SimpleNamespace(
        is_configured=True,
        validate_token=lambda token: True,
        get_username_for_token=lambda token: username,
        users={username: {"must_change_password": flagged, "is_admin": False}},
        get_privileges=lambda u: {},
        is_admin=lambda u: False,
    )


def _patch_gate_auth(monkeypatch, app_module_instance, *, username, flagged):
    fake_auth = _fake_gate_auth_manager(username=username, flagged=flagged)
    monkeypatch.setattr(app_module_instance, "auth_manager", fake_auth)
    monkeypatch.setattr(app_module_instance.app.state, "auth_manager", fake_auth)


def test_flagged_user_blocked_on_non_allowlisted_path(app_module_instance, monkeypatch):
    _patch_gate_auth(monkeypatch, app_module_instance, username="bob", flagged=True)
    client = TestClient(app_module_instance.app)
    client.cookies.set("prisma_session", "sometoken")

    response = client.get("/api/auth/2fa/status")

    assert response.status_code == 403
    assert response.json().get("error") == "must_change_password"


def test_flagged_user_allowed_on_change_password_path(app_module_instance, monkeypatch):
    _patch_gate_auth(monkeypatch, app_module_instance, username="bob", flagged=True)
    client = TestClient(app_module_instance.app)
    client.cookies.set("prisma_session", "sometoken")

    response = client.post(
        "/api/auth/change-password",
        json={"current_password": "x", "new_password": "y1234567"},
    )

    assert not (response.status_code == 403 and response.json().get("error") == "must_change_password")


def test_flagged_user_allowed_on_logout_and_status_paths(app_module_instance, monkeypatch):
    _patch_gate_auth(monkeypatch, app_module_instance, username="bob", flagged=True)
    client = TestClient(app_module_instance.app)
    client.cookies.set("prisma_session", "sometoken")

    logout_response = client.post("/api/auth/logout")
    status_response = client.get("/api/auth/status")

    assert logout_response.status_code == 200
    assert status_response.status_code == 200


def test_non_flagged_user_not_blocked_by_gate(app_module_instance, monkeypatch):
    _patch_gate_auth(monkeypatch, app_module_instance, username="bob", flagged=False)
    client = TestClient(app_module_instance.app)
    client.cookies.set("prisma_session", "sometoken")

    response = client.get("/api/auth/2fa/status")

    assert not (response.status_code == 403 and response.json().get("error") == "must_change_password")
