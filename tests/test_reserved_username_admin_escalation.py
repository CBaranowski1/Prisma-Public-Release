"""Regression: reserved sentinel usernames must not be registerable.

`core.middleware.require_admin` grants admin to any request whose
`current_user == "internal-tool"` (the in-process tool-loopback sentinel),
and the cookie auth path in app.py sets `current_user` to the raw username.
Before this fix nothing reserved that name, so a self-service signup (or an
admin typo) creating the account "internal-tool" was silently treated as an
admin by every `require_admin`-gated route — a privilege escalation. "api"
is reserved for the same reason (bearer-token owner attribution collision).

See the privilege-escalation finding from the 2026-06 code review.
"""

import pytest


def _fresh_auth_manager(tmp_path):
    # DEBT-03 (33-08): previously did a raw sys.modules.pop("core.auth",
    # None) + delattr(core, "auth") + reimport dance (Pitfall 1) on every
    # test invocation (12 parametrized calls), never reverted -- each call
    # bound a brand-new core.auth module object into sys.modules/the core
    # package attribute, permanently diverging it from whatever other
    # already-imported code held a reference to the original AuthManager
    # class. Confirmed via 33-08 bisection as one cause of the
    # test_tool_run_query.py / test_verify_qb_spine_integration.py
    # order-dependent failures (2-file pairing: 4 failed -> 0). No other
    # test file stubs core/core.auth, so the forcing dance was unneeded;
    # use the real, already-loaded module directly.
    from core.auth import AuthManager

    return AuthManager(str(tmp_path / "auth.json"))


@pytest.mark.parametrize(
    "name",
    ["internal-tool", "api", "demo", "system", "INTERNAL-TOOL", " Internal-Tool ", "Api", "SYSTEM"],
)
def test_create_user_rejects_reserved_usernames(tmp_path, name):
    mgr = _fresh_auth_manager(tmp_path)
    assert mgr.create_user(name, "pw-123456") is False
    # The normalized name must not have been written to the user table.
    assert name.strip().lower() not in mgr.users


def test_create_user_rejects_empty_username(tmp_path):
    mgr = _fresh_auth_manager(tmp_path)
    assert mgr.create_user("   ", "pw-123456") is False
    assert "" not in mgr.users


def test_setup_rejects_reserved_admin_username(tmp_path):
    mgr = _fresh_auth_manager(tmp_path)
    # First-run admin setup funnels through create_user, so it's covered too.
    assert mgr.setup("internal-tool", "pw-123456") is False
    assert mgr.is_configured is False


def test_rename_into_reserved_username_is_blocked(tmp_path):
    mgr = _fresh_auth_manager(tmp_path)
    assert mgr.create_user("admin", "pw-123456", is_admin=True) is True
    assert mgr.create_user("bob", "pw-123456") is True
    assert mgr.rename_user("bob", "internal-tool", "admin") is False
    assert "internal-tool" not in mgr.users
    assert "bob" in mgr.users


def test_normal_usernames_still_allowed(tmp_path):
    mgr = _fresh_auth_manager(tmp_path)
    assert mgr.create_user("alice", "pw-123456") is True
    assert "alice" in mgr.users
