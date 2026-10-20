"""SC-5 (D-08): the assembled-app integration proof for Phase 17's trust-
boundary hardening and privilege-gating flip -- the named "integration-
checker" artifact honoring this project's "presence != integration"
principle for SEC-18/25/26/27.

Every load-bearing assertion in this file is driven through the REAL,
composed FastAPI `app` object via `starlette.testclient.TestClient`, past the
real `AuthMiddleware` / `require_admin` / `is_public_blocked_tool` /
`owner_is_admin_or_single_user` -- never by importing `core.auth` or
`src.tool_security` to assert behavior directly. Where a proof needs a test
seam (faking the auth manager's data, or driving `execute_tool_block` -- the
real tool-execution entry point -- with a synthetic tool name), that mirrors
the exact test-only-shim discipline already established in
`tests/test_verify_qb_privilege.py` / `tests/test_verify_companion_pairing_integration.py`:
monkeypatch the object the middleware/gate itself reads, never the assertion
target's own internals.

Three sub-proofs (D-08a/b/c):

  (a) SC-5a -- a forged proxy/tunnel header (`True-Client-IP`, `X-Client-IP`,
      `CF-Connecting-IP`, `X-Forwarded-For`, plus a HELD-OUT header never
      enumerated in `17-CONTEXT.md`'s explicit lists, `X-Foo-Real-Ip`) sent
      from a loopback peer is NOT treated as trusted-loopback -- proven
      against `/api/auth/switch-user` (no rate limiter, so the full family
      can be parametrized freely) and, for one representative header, against
      the first-run `/api/auth/setup` gate too. A genuine direct-loopback
      call with zero forwarding headers still succeeds (backward-compat
      guard -- CONTEXT.md "Specific Ideas").

  (b) SC-5b -- a brand-new, never-allowlisted tool name is default-DENIED
      for a non-admin owner through `execute_tool_block` (the real
      tool-execution path, `src/tool_execution.py`), and pre-setup (auth
      enabled, zero users) does NOT fail-open for it either.

  (c) SC-5c -- `require_admin`'s internal-tool grant (proven here against the
      real admin-gated `GET /api/embeddings/models` route) is available ONLY
      when AuthMiddleware's validated token+loopback branch set the
      middleware-only `request.state.is_internal_tool` flag; a forged
      loopback header defeats the token bypass just like SC-5a; and a
      `current_user == "internal-tool"` string reached via the ORDINARY
      cookie-auth path (flag never set) is refused on the same admin route.

Order-independence (Phase-16 SC-4 precedent): `_heal_polluted_modules` evicts
BOTH MagicMock-typed AND synthetic (no on-disk `__spec__.origin`) leaked
stubs of `core.database` / `core.auth` / `src.endpoint_resolver` /
`src.request_models` / `core.models` / `core.session_manager` before
`import app`, copied verbatim from
`tests/test_verify_companion_pairing_integration.py:56-96` -- proven to pass
standalone AND after `tests/test_companion_pairing.py` (whose autouse fixture
is the one that leaks synthetic stubs) AND in the full suite.
"""
import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Assembled-app harness (reused verbatim from
# tests/test_verify_companion_pairing_integration.py:56-96 -- the UPGRADED
# healer that also evicts synthetic, non-MagicMock stub modules, not just
# tests/test_verify_qb_privilege.py's original MagicMock-only version. Kept
# inline per that file's own note that importing a fixture across test
# modules is not reliable.)
# ---------------------------------------------------------------------------


def _heal_polluted_modules():
    """Sibling unit tests stub model modules (src.request_models, core.models)
    with MagicMock and import route modules against them, leaving routes.*
    bound to mocks in sys.modules. That makes app route-registration raise
    FastAPIError when we build the full app. Drop mock-contaminated model
    modules, then force app + all routes.* to re-import so they rebind
    against the real models.

    tests/test_companion_pairing.py's autouse fixture raw-inserts synthetic
    stub modules (core.database / core.auth / src.endpoint_resolver) into
    sys.modules and leaks them (its monkeypatch.setitem records the stub it
    just inserted as the restore baseline, so teardown re-installs the stub
    rather than deleting it). Those stubs expose only a partial surface, so
    a later `import app` -> routes.* can raise ImportError under that
    collection order. They are NOT MagicMock-typed, so the loop above misses
    them; evict any synthetic (no on-disk origin) copy so the real module
    re-imports and the assembled app builds regardless of test order."""
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    for name in ("core.database", "core.auth", "src.endpoint_resolver"):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        spec = getattr(mod, "__spec__", None)
        origin = getattr(spec, "origin", None) if spec is not None else None
        if origin is None or getattr(mod, "__file__", None) is None:
            sys.modules.pop(name, None)
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def app_module_instance():
    """Build the assembled app with AUTH_ENABLED forced on, so the real
    AuthMiddleware / require_admin / rate limiters are on the request path
    instead of bypassed -- otherwise every gate this file proves would be a
    no-op (mirrors T-09-04 for the assembled-app claim)."""
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    # Environment gap (DEBT-03, out of Phase-17 scope): bs4 is a documented
    # dependency (requirements.txt) but not installed in this venv. app.py's
    # import chain hard-imports it. Stub it ONLY for the duration of building
    # the assembled app here, and ONLY if genuinely absent, then remove the
    # stub immediately after.
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


def _bypass_auth_as_admin(app_module_instance, monkeypatch):
    """Instance-level patch (established pattern, see STATE.md Phase 9 P5 and
    tests/test_dev_mint_hard_gate.py:124-141): mutate the REAL shared
    auth_manager instance's methods rather than replacing the object.
    routes/auth_routes.py's `setup_auth_routes(auth_manager)` closure
    captured this exact instance at app.py import time -- rebinding the
    module-level `auth_manager` attribute to a NEW fake object would leave
    that closure pointing at the stale original, and the route would never
    observe our admin-authenticated shim."""
    real_auth = app_module_instance.auth_manager
    monkeypatch.setattr(type(real_auth), "is_configured", property(lambda self: True))
    monkeypatch.setattr(real_auth, "validate_token", lambda token: True)
    monkeypatch.setattr(real_auth, "get_username_for_token", lambda token: "admin")
    monkeypatch.setattr(real_auth, "is_admin", lambda u: True)
    return real_auth


# ---------------------------------------------------------------------------
# SC-5a: forged proxy header from a loopback peer is NOT trusted-loopback.
# ---------------------------------------------------------------------------

# The 4 headers named in 17-CONTEXT.md D-01, plus ONE held-out header never
# enumerated in that list (X-Foo-Real-Ip -- matches the vetted `*-real-ip`
# structural suffix rule per 17-RESEARCH.md's own suggested example) --
# proves detection is genuinely structural/pattern-based, not a re-encoded
# enumerated denylist under a different name.
FORGED_LOOPBACK_HEADERS = [
    ("True-Client-IP", "1.2.3.4"),
    ("X-Client-IP", "1.2.3.4"),
    ("CF-Connecting-IP", "1.2.3.4"),
    ("X-Forwarded-For", "1.2.3.4"),
    ("X-Foo-Real-Ip", "1.2.3.4"),  # held-out: never named in 17-CONTEXT.md
]


@pytest.mark.parametrize("header_name,header_value", FORGED_LOOPBACK_HEADERS)
def test_switch_user_denies_forged_proxy_header_from_loopback_peer(
    app_module_instance, monkeypatch, header_name, header_value
):
    """A loopback-peer connection (client.host == 127.0.0.1) carrying ANY
    forwarding-shaped header -- including one never named anywhere in this
    phase's context docs -- must be refused by the /switch-user loopback
    gate, exactly as a Cloudflare-tunneled / reverse-proxied remote caller
    would present."""
    _bypass_auth_as_admin(app_module_instance, monkeypatch)
    monkeypatch.setenv("PRISMA_DEV_SWITCHER", "1")
    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(
        app_module_instance.app,
        client=("127.0.0.1", 12345),
        cookies={SESSION_COOKIE: "sc5-faketoken"},
    )
    resp = client.post(
        "/api/auth/switch-user",
        json={"username": "coder"},
        headers={header_name: header_value},
    )
    assert resp.status_code == 403


def test_switch_user_allows_genuine_direct_loopback_with_zero_forwarding_headers(
    app_module_instance, monkeypatch
):
    """Backward-compat guard (17-CONTEXT.md "Specific Ideas"): a legitimate
    direct-loopback call with NO forwarding headers must still pass the
    trust-boundary gate -- proving the structural check did not over-tighten
    and break local admin/setup. The target username doesn't exist in this
    test environment, so the route's OWN business logic 404s afterward; the
    load-bearing assertion is that the gate itself did not 403 it."""
    _bypass_auth_as_admin(app_module_instance, monkeypatch)
    monkeypatch.setenv("PRISMA_DEV_SWITCHER", "1")
    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(
        app_module_instance.app,
        client=("127.0.0.1", 12345),
        cookies={SESSION_COOKIE: "sc5-faketoken"},
    )
    resp = client.post("/api/auth/switch-user", json={"username": "coder"})
    assert resp.status_code != 403


def test_first_run_setup_denies_held_out_forged_header_from_loopback_peer(
    app_module_instance, monkeypatch
):
    """The SAME structural closure applies to a second, independent
    loopback-gated call site: first-run /api/auth/setup. Uses the held-out
    header (never enumerated) so this is not just a repeat of the
    /switch-user proof against a different name -- it proves the CLASS
    closure holds across call sites, not just the one gate exercised above."""
    real_auth = app_module_instance.auth_manager
    monkeypatch.setattr(type(real_auth), "is_configured", property(lambda self: False))
    monkeypatch.delenv("PRISMA_ADMIN_PASSWORD", raising=False)

    client = TestClient(app_module_instance.app, client=("127.0.0.1", 12345))
    resp = client.post(
        "/api/auth/setup",
        json={"username": "newadmin", "password": "longenoughpassword"},
        headers={"X-Foo-Real-Ip": "1.2.3.4"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# SC-5b: a brand-new, never-allowlisted tool default-denies for a non-admin
# owner; pre-setup does not fail-open either. Driven through
# execute_tool_block -- the real tool-execution entry point -- not by
# calling is_public_blocked_tool/owner_is_admin_or_single_user directly.
# ---------------------------------------------------------------------------

NEW_PRIVILEGED_TOOL_NAME = "totally_new_privileged_tool_never_allowlisted_sc5"


class _FakeAuthConfiguredNonAdmin:
    """Test-only stand-in (T-09-03 discipline): auth IS configured, and the
    caller is not an admin -- the ordinary post-setup state."""

    is_configured = True

    def is_admin(self, username):
        return False


class _FakeAuthPreSetup:
    """Test-only stand-in: zero users exist yet (pre-setup window)."""

    is_configured = False

    def is_admin(self, username):
        return False


@pytest.mark.asyncio
async def test_new_nonallowlisted_tool_default_denied_for_nonadmin_owner(monkeypatch):
    """A tool name that has never been allowlisted anywhere (not the old
    denylist's complement, not PUBLIC_TOOL_ALLOWLIST) must default-DENY for a
    real, configured, non-admin owner through the real tool-execution path
    (SEC-27a)."""
    import core.auth as core_auth_mod

    monkeypatch.setenv("AUTH_ENABLED", "true")
    # Patch the AuthManager CLASS (not get_auth_manager itself) so
    # get_auth_manager()'s lazy, call-time `AuthManager(auth_path)`
    # construction (core/auth.py, Phase 17-04) resolves against this fake --
    # the autouse tests/conftest.py fixture resets the singleton cache before
    # this test runs, so the very first call constructs via this patch.
    monkeypatch.setattr(core_auth_mod, "AuthManager", lambda *a, **kw: _FakeAuthConfiguredNonAdmin())

    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block

    block = ToolBlock(NEW_PRIVILEGED_TOOL_NAME, "")
    desc, result = await execute_tool_block(block, owner="bob-nonadmin")

    assert desc == f"{NEW_PRIVILEGED_TOOL_NAME}: BLOCKED"
    assert result["exit_code"] == 1
    assert "restricted to admin users" in result["error"]


@pytest.mark.asyncio
async def test_presetup_zero_users_does_not_failopen_privileged_tool(monkeypatch):
    """Pre-setup (auth enabled, zero users) must NOT fail-open for a
    privileged tool -- this is D-07b/SEC-27b's load-bearing claim, proven
    here through the same real tool-execution path used above, not by
    calling owner_is_admin_or_single_user directly."""
    import core.auth as core_auth_mod

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(core_auth_mod, "AuthManager", lambda *a, **kw: _FakeAuthPreSetup())

    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block

    block = ToolBlock(NEW_PRIVILEGED_TOOL_NAME, "")
    # No session/owner exists yet in the pre-setup window.
    desc, result = await execute_tool_block(block, owner=None)

    assert desc == f"{NEW_PRIVILEGED_TOOL_NAME}: BLOCKED"
    assert result["exit_code"] == 1
    assert "restricted to admin users" in result["error"]


@pytest.mark.asyncio
async def test_auth_disabled_still_grants_privileged_tool_non_regression(monkeypatch):
    """Non-regression (17-CONTEXT.md "Specific Ideas" #2 -- the single most
    important backward-compat guard): explicit AUTH_ENABLED=false (the
    intentional no-auth mode) must still grant a privileged (non-allowlisted)
    tool, even though the AuthManager reports unconfigured/no-admin. This
    proves SEC-27b's fix distinguishes "explicitly disabled" from
    "pre-setup". Uses `manage_tasks` (a real, non-allowlisted tool with a
    working handler, unlike the synthetic never-implemented tool name used
    above -- that name would report exit_code 1 "Unknown tool type"
    regardless of admin status, which would prove nothing here) so a
    genuine exit_code 0 is reachable."""
    import core.auth as core_auth_mod

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(core_auth_mod, "AuthManager", lambda *a, **kw: _FakeAuthPreSetup())

    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block

    block = ToolBlock("manage_tasks", '{"action":"list"}')
    desc, result = await execute_tool_block(block, owner=None)

    assert result["exit_code"] == 0
    assert desc != "manage_tasks: BLOCKED"


# ---------------------------------------------------------------------------
# SC-5c: require_admin grants the internal-tool path ONLY via the
# middleware-only is_internal_tool flag + re-asserted loopback -- proven
# against the real admin-gated GET /api/embeddings/models route (router-level
# `dependencies=[Depends(require_admin)]`, routes/embedding_routes.py).
# ---------------------------------------------------------------------------


def test_internal_tool_token_plus_genuine_loopback_grants_admin_gated_route(
    app_module_instance, monkeypatch
):
    """A request from a genuine loopback peer carrying the valid internal-tool
    token (no forwarding headers) is the ONE path that legitimately grants
    the internal-tool admin bypass -- AuthMiddleware validates
    compare_digest(token)+is_trusted_loopback, sets request.state.is_internal_tool,
    and require_admin grants on that flag + its own re-asserted loopback
    check. The route's OWN handler may still 503 (fastembed not installed in
    this environment, per .claude/CLAUDE.md's documented env gap) -- the
    load-bearing assertion is that it is NOT refused at the require_admin
    gate (403) or the auth gate (401)."""
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    client = TestClient(app_module_instance.app, client=("127.0.0.1", 12345))
    resp = client.get(
        "/api/embeddings/models",
        headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
    )
    assert resp.status_code not in (401, 403)


def test_internal_tool_token_defeated_by_forged_loopback_header(app_module_instance, monkeypatch):
    """The SAME internal-tool token, from the SAME loopback peer.host, but
    with a forged forwarding header present, must NOT be granted the
    internal-tool bypass -- AuthMiddleware's own is_trusted_loopback check
    (which the token-bypass branch requires) rejects it exactly as SC-5a
    proved for /switch-user, so the request falls through to ordinary
    (here: absent) auth and is refused."""
    from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN

    client = TestClient(app_module_instance.app, client=("127.0.0.1", 12345))
    resp = client.get(
        "/api/embeddings/models",
        headers={
            INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN,
            "True-Client-IP": "1.2.3.4",
        },
    )
    assert resp.status_code in (401, 403)


def test_spoofed_current_user_internal_tool_string_without_flag_denied(
    app_module_instance, monkeypatch
):
    """A request that reaches AuthMiddleware's ORDINARY cookie-auth branch
    (never the validated token+loopback bypass branch) and happens to
    resolve current_user to the literal string "internal-tool" must NOT be
    granted admin -- require_admin checks the middleware-only
    is_internal_tool FLAG (never set on this path), not the current_user
    string (SEC-26/D-05). Proves the fix through the real cookie-auth path,
    not by hand-constructing request.state."""
    real_auth = app_module_instance.auth_manager
    monkeypatch.setattr(type(real_auth), "is_configured", property(lambda self: True))
    monkeypatch.setattr(real_auth, "validate_token", lambda token: True)
    monkeypatch.setattr(real_auth, "get_username_for_token", lambda token: "internal-tool")
    monkeypatch.setattr(real_auth, "is_admin", lambda u: False)

    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(
        app_module_instance.app,
        cookies={SESSION_COOKIE: "sc5-spoofed-string-faketoken"},
    )
    resp = client.get("/api/embeddings/models")
    assert resp.status_code == 403
