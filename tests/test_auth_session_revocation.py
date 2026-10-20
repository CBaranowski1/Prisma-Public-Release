"""Regression tests for password-change session revocation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


# DEBT-03 (33-08): this file previously forced a "real" core.auth via a raw
# sys.modules.pop("core.auth", None) + delattr(core, "auth") + reimport
# dance (Pitfall 1), then permanently overwrote auth_mod._hash_password /
# _verify_password with raw attribute assignment (Pitfall 2) -- neither
# mutation was ever reverted, leaking a permanently-fake, unsalted hashing
# scheme into core.auth for the rest of any pytest process that collected
# this file. Verified no other test file stubs core/core.auth/
# routes.auth_routes in sys.modules, so the forcing dance was unneeded here
# too (same shape/fix as 33-03's test_auth_config_lock_concurrency.py and
# 33-08's test_auth_recovery.py). Fixed by using the real, already-loaded
# modules directly and applying any fake-hashing swap via
# monkeypatch.setattr so pytest auto-reverts it at teardown.

def _make_manager(tmp_path, monkeypatch):
    import core.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")
    monkeypatch.setattr(auth_mod, "_verify_password", lambda password, hashed: hashed == f"hash:{password}")
    auth_path = tmp_path / "auth.json"
    mgr = auth_mod.AuthManager(str(auth_path))
    assert mgr.create_user("alice", "old-password", is_admin=False)
    assert mgr.create_user("bob", "bob-password", is_admin=False)
    return mgr


async def _immediate_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def test_revoke_user_sessions_preserves_current_and_persists(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)
    current = mgr.create_session("alice", "old-password")
    other = mgr.create_session("alice", "old-password")
    bob = mgr.create_session("bob", "bob-password")

    revoked = mgr.revoke_user_sessions("alice", except_token=current)

    assert revoked == 1
    assert mgr.validate_token(current) is True
    assert mgr.validate_token(other) is False
    assert mgr.validate_token(bob) is True


def test_wrong_current_password_does_not_revoke_sessions(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)
    current = mgr.create_session("alice", "old-password")
    other = mgr.create_session("alice", "old-password")

    assert mgr.change_password("alice", "wrong-password", "new-password") is False

    assert mgr.validate_token(current) is True
    assert mgr.validate_token(other) is True


def test_password_change_allows_new_password_and_blocks_old_password(tmp_path, monkeypatch):
    mgr = _make_manager(tmp_path, monkeypatch)

    assert mgr.change_password("alice", "old-password", "new-password") is True

    assert mgr.create_session("alice", "old-password") is None
    assert mgr.create_session("alice", "new-password") is not None


def _change_password_endpoint(auth_manager):
    from routes.auth_routes import ChangePasswordRequest, setup_auth_routes

    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/change-password":
            return route.endpoint, ChangePasswordRequest
    raise AssertionError("change-password route not found")


def test_change_password_route_revokes_other_sessions_after_success(monkeypatch):
    auth = MagicMock()
    auth.get_username_for_token.return_value = "alice"
    auth.change_password.return_value = True
    endpoint, ChangePasswordRequest = _change_password_endpoint(auth)
    monkeypatch.setattr(
        "routes.auth_routes.asyncio.to_thread",
        lambda fn, *args, **kwargs: _immediate_to_thread(fn, *args, **kwargs),
    )
    request = SimpleNamespace(cookies={"prisma_session": "current-token"})
    body = ChangePasswordRequest(current_password="old-password", new_password="new-password")

    result = asyncio.run(endpoint(body=body, request=request))

    assert result == {"ok": True}
    auth.change_password.assert_called_once_with("alice", "old-password", "new-password")
    auth.revoke_user_sessions.assert_called_once_with("alice", "current-token")


def test_change_password_route_wrong_password_does_not_revoke(monkeypatch):
    auth = MagicMock()
    auth.get_username_for_token.return_value = "alice"
    auth.change_password.return_value = False
    endpoint, ChangePasswordRequest = _change_password_endpoint(auth)
    monkeypatch.setattr(
        "routes.auth_routes.asyncio.to_thread",
        lambda fn, *args, **kwargs: _immediate_to_thread(fn, *args, **kwargs),
    )
    request = SimpleNamespace(cookies={"prisma_session": "current-token"})
    body = ChangePasswordRequest(current_password="wrong-password", new_password="new-password")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(body=body, request=request))

    assert exc.value.status_code == 400
    auth.revoke_user_sessions.assert_not_called()
