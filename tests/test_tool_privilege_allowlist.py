"""SEC-27a/SEC-27b: PUBLIC_TOOL_ALLOWLIST default-deny flip + fail-closed
pre-setup for owner_is_admin_or_single_user / routes.task_routes._is_admin.

Covers 17-05-PLAN.md's `<behavior>` blocks for both tasks:
  Task 1 (SEC-27a) - is_public_blocked_tool is now allowlist-based
    default-deny: allowlisted tools pass, everything else (new tools,
    mcp__*, the 7 ex-ungated agent_loop tools, non-string) blocks; run_query
    is excluded from the allowlist (its own gate handles it).
  Task 2 (SEC-27b) - owner_is_admin_or_single_user / task_routes._is_admin
    grant ONLY on explicit AUTH_ENABLED=false (_auth_disabled()) or admin
    owner; pre-setup (auth enabled, zero users) DENIES.
"""
import pytest

from src.tool_security import (
    PUBLIC_TOOL_ALLOWLIST,
    is_public_blocked_tool,
    owner_is_admin_or_single_user,
)


# ---------------------------------------------------------------------------
# Task 1: PUBLIC_TOOL_ALLOWLIST default-deny flip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name",
    sorted(PUBLIC_TOOL_ALLOWLIST),
)
def test_allowlisted_tool_is_not_blocked(tool_name):
    assert is_public_blocked_tool(tool_name) is False


def test_brand_new_unknown_tool_default_denies():
    assert is_public_blocked_tool("brand_new_privileged_tool") is True


def test_mcp_namespaced_tool_default_denies():
    assert is_public_blocked_tool("mcp__anything") is True
    assert is_public_blocked_tool("mcp__filesystem__read_file") is True


@pytest.mark.parametrize(
    "tool_name",
    [
        "create_session",
        "list_sessions",
        "send_to_session",
        "pipeline",
        "manage_session",
        "list_models",
        "ask_teacher",
    ],
)
def test_seven_ex_ungated_agent_loop_tools_now_deny(tool_name):
    """Closes 17-RESEARCH.md Summary finding #1 -- these 7 tools were never
    gated at execution time before this plan. They must land on the deny
    side (i.e. NOT be members of PUBLIC_TOOL_ALLOWLIST)."""
    assert tool_name not in PUBLIC_TOOL_ALLOWLIST
    assert is_public_blocked_tool(tool_name) is True


def test_none_or_empty_is_not_gated():
    assert is_public_blocked_tool(None) is False
    assert is_public_blocked_tool("") is False


def test_nonstring_fail_closed():
    assert is_public_blocked_tool(123) is True
    assert is_public_blocked_tool(["bash"]) is True


def test_run_query_excluded_from_allowlist_and_blocked_here():
    """run_query keeps its OWN owner_can_manage_data_sources gate
    (tests/test_tool_run_query.py) -- it must never be granted by blanket
    allowlist membership, so it correctly shows as blocked at this generic
    gate too."""
    assert "run_query" not in PUBLIC_TOOL_ALLOWLIST
    assert is_public_blocked_tool("run_query") is True


def test_tool_execution_admin_tools_deleted():
    """The redundant tool_execution.py _ADMIN_TOOLS enforcement set is gone
    -- enforcement is solely is_public_blocked_tool."""
    import src.tool_execution as te

    assert not hasattr(te, "_ADMIN_TOOLS")


# ---------------------------------------------------------------------------
# Task 2: owner_is_admin_or_single_user fail-closed pre-setup
# ---------------------------------------------------------------------------

class _StubAuthManager:
    def __init__(self, admins=None):
        self._admins = set(admins or [])

    def is_admin(self, owner):
        return owner in self._admins


def test_auth_disabled_grants_regardless_of_owner(monkeypatch):
    """AUTH_ENABLED=false (explicit no-auth mode) must keep working -- the
    ONLY open path besides an actual admin owner."""
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True)
    assert owner_is_admin_or_single_user(None) is True
    assert owner_is_admin_or_single_user("") is True
    assert owner_is_admin_or_single_user("anyone") is True


def test_presetup_zero_users_denies_even_with_auth_enabled(monkeypatch):
    """Pre-setup: auth is ENABLED (AUTH_ENABLED not disabled) but zero users
    exist yet. This must DENY -- closing the old unconfigured-auth
    fail-open bare grant (SEC-27b)."""
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: False)
    monkeypatch.setattr(
        "core.auth.get_auth_manager", lambda: _StubAuthManager(admins=set())
    )
    assert owner_is_admin_or_single_user("anyone") is False
    assert owner_is_admin_or_single_user(None) is False
    assert owner_is_admin_or_single_user("") is False


def test_owner_is_admin_allows(monkeypatch):
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: False)
    monkeypatch.setattr(
        "core.auth.get_auth_manager", lambda: _StubAuthManager(admins={"alice"})
    )
    assert owner_is_admin_or_single_user("alice") is True


def test_owner_non_admin_denies(monkeypatch):
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: False)
    monkeypatch.setattr(
        "core.auth.get_auth_manager", lambda: _StubAuthManager(admins={"alice"})
    )
    assert owner_is_admin_or_single_user("bob") is False


def test_exception_branch_fails_closed(monkeypatch):
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: False)

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr("core.auth.get_auth_manager", _raise)
    assert owner_is_admin_or_single_user("anyone") is False


# ---------------------------------------------------------------------------
# Task 2: routes.task_routes._is_admin mirrors the same fail-closed shape
# (RESEARCH.md Open Question 1). `_is_admin` is a closure inside
# `setup_task_routes`, so it's exercised through the real
# `GET /api/tasks/meta/actions` endpoint (pure computation, no DB access),
# which filters _ADMIN_ONLY_ACTIONS-listed builtin actions through it.
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self, current_user):
        self.current_user = current_user


class _FakeRequest:
    def __init__(self, current_user):
        self.state = _FakeState(current_user)


def _list_actions_endpoint():
    import routes.task_routes as task_routes

    router = task_routes.setup_task_routes(task_scheduler=None)
    for route in router.routes:
        if route.path == "/api/tasks/meta/actions":
            return route.endpoint
    raise AssertionError("GET /api/tasks/meta/actions route not found")


@pytest.mark.asyncio
async def test_task_routes_presetup_zero_users_hides_admin_only_actions(monkeypatch):
    """Pre-setup (auth enabled, zero users): _is_admin must DENY, so
    admin-only builtin actions (run_script/ssh_command -- the two
    _ADMIN_ONLY_ACTIONS entries present in BUILTIN_ACTION_INFO) are filtered
    out of the listing for a non-admin/any owner."""
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: False)
    monkeypatch.setattr(
        "core.auth.get_auth_manager", lambda: _StubAuthManager(admins=set())
    )
    endpoint = _list_actions_endpoint()
    result = await endpoint(_FakeRequest("someone"))
    names = {a["name"] for a in result["actions"]}
    assert "run_script" not in names
    assert "ssh_command" not in names


@pytest.mark.asyncio
async def test_task_routes_auth_disabled_grants_admin_only_actions(monkeypatch):
    """AUTH_ENABLED=false (explicit no-auth mode) must keep working for
    task_routes._is_admin too -- admin-only actions are visible."""
    monkeypatch.setattr("src.auth_helpers._auth_disabled", lambda: True)
    endpoint = _list_actions_endpoint()
    result = await endpoint(_FakeRequest(""))
    names = {a["name"] for a in result["actions"]}
    assert "run_script" in names
    assert "ssh_command" in names


def test_task_routes_no_longer_fail_opens_on_unconfigured():
    """Source-level regression: the literal `is_configured` fail-open check
    must be gone from task_routes.py's _is_admin, mirroring the SEC-27b fix
    applied to owner_is_admin_or_single_user."""
    import inspect
    import routes.task_routes as task_routes

    src = inspect.getsource(task_routes)
    assert "_auth_disabled" in src
    # The old fail-open condition (`auth.is_configured` gating a bare grant)
    # must not appear anywhere in the module's live code.
    assert "auth.is_configured" not in src
