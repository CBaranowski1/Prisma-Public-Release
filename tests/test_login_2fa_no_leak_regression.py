"""SEC-21 D-12 regression lock: `/login` already checks the password FIRST
and only ever returns a `requires_totp` signal AFTER the password has been
confirmed correct. This file does NOT change `/login` -- 15-RESEARCH.md
Pitfall 10 explicitly warns against "fixing" already-correct code; this is a
verification + regression-test task only, so a future edit can't silently
reorder the check and reintroduce a 2FA-state oracle for account enumeration.

Deferred (out of phase scope, RESEARCH Open Question 1 / Pitfall 11): the
pre-existing `verify_password` username-enumeration TIMING side-channel
(CWE-204, unknown username short-circuits fast vs. a known username paying
the full bcrypt cost) is a real, adjacent, but separately-scoped finding.
No assertion for it lives in this file -- it is a documented deferral only.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def _login_endpoint(auth_manager):
    from routes.auth_routes import setup_auth_routes

    router = setup_auth_routes(auth_manager)
    for route in router.routes:
        if getattr(route, "path", "") == "/api/auth/login" and "POST" in (getattr(route, "methods", None) or set()):
            return route.endpoint
    raise AssertionError("POST /api/auth/login route not registered")


def _fake_request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def _fake_response():
    return SimpleNamespace(set_cookie=lambda **kw: None)


def test_wrong_password_2fa_enabled_user_gets_generic_401_no_leak():
    """A wrong password for a 2FA-enabled account must fail BEFORE any 2FA
    state is even queried -- proven by asserting `totp_enabled` is never
    called, not just that the response body happens to omit the key."""
    from routes.auth_routes import LoginRequest

    auth = MagicMock()
    auth.verify_password.return_value = False
    auth.totp_enabled.return_value = True  # would leak 2FA state if reached

    login = _login_endpoint(auth)
    body = LoginRequest(username="alice", password="wrong-password")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(login(body=body, request=_fake_request(), response=_fake_response()))

    assert exc.value.status_code == 401
    detail = str(exc.value.detail).lower()
    # Generic credentials-invalid message -- no mention of 2FA/TOTP state.
    assert "totp" not in detail
    assert "2fa" not in detail
    assert "requires_totp" not in detail
    # Password-first ordering (D-12): 2FA state is never even queried on a
    # wrong password, so there is no signal to leak in the first place.
    auth.totp_enabled.assert_not_called()
    auth.create_session.assert_not_called()


def test_correct_password_2fa_enabled_no_code_returns_requires_totp():
    """Once the password is confirmed correct, the client legitimately needs
    to know a TOTP code is required -- this is the expected, non-leaking
    shape of the requires_totp signal (post-password-verification only)."""
    from routes.auth_routes import LoginRequest

    auth = MagicMock()
    auth.verify_password.return_value = True
    auth.totp_enabled.return_value = True

    login = _login_endpoint(auth)
    body = LoginRequest(username="alice", password="correct-password")

    out = asyncio.run(login(body=body, request=_fake_request(), response=_fake_response()))

    assert out == {"ok": False, "requires_totp": True, "username": "alice"}
    # No session minted until the TOTP step also succeeds.
    auth.create_session.assert_not_called()


def test_wrong_password_no_2fa_user_still_generic_401():
    """Non-2FA accounts must also get the same generic 401 shape on a wrong
    password -- no behavioral fork based on whether 2FA would have applied."""
    from routes.auth_routes import LoginRequest

    auth = MagicMock()
    auth.verify_password.return_value = False
    auth.totp_enabled.return_value = False

    login = _login_endpoint(auth)
    body = LoginRequest(username="bob", password="wrong-password")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(login(body=body, request=_fake_request(), response=_fake_response()))

    assert exc.value.status_code == 401
    auth.totp_enabled.assert_not_called()
