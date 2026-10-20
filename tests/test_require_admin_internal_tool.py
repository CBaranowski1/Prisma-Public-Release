"""RED->GREEN suite for SEC-26 (D-05/D-06): `require_admin` must grant the
internal-tool path ONLY via a dedicated middleware-only `request.state.is_internal_tool`
flag (plus a re-asserted `is_trusted_loopback`), never via the bare
`current_user == "internal-tool"` string on shared, spoofable `request.state`.

Exercises `core.middleware.require_admin` directly with minimal fake Request
stand-ins (a real `starlette.datastructures.Headers` for header lookups),
mirroring `tests/test_trust_boundary_core.py`'s `_FakeRequest` convention --
no assembled app needed for this unit-level proof.
"""
import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN, require_admin


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeState:
    """Minimal request.state stand-in. `is_internal_tool` is only set on the
    instance when explicitly requested, so the "attribute entirely absent"
    shape (the realistic case for a request that never passed through
    AuthMiddleware's internal-tool branch) can be exercised via getattr's
    default, not merely a `False` value."""

    def __init__(self, current_user=None, is_internal_tool=None):
        self.current_user = current_user
        if is_internal_tool is not None:
            self.is_internal_tool = is_internal_tool


class _FakeAuthManager:
    is_configured = True

    def is_admin(self, user):
        return False


class _FakeAppState:
    def __init__(self, auth_manager=None):
        self.auth_manager = auth_manager or _FakeAuthManager()


class _FakeApp:
    def __init__(self, auth_manager=None):
        self.state = _FakeAppState(auth_manager)


class _FakeRequest:
    """Stand-in exposing exactly what require_admin/is_trusted_loopback read:
    `.headers` (real Starlette Headers), `.state`, `.app.state.auth_manager`,
    `.client.host`."""

    def __init__(self, host="127.0.0.1", headers=None, state=None, auth_manager=None):
        self.client = _FakeClient(host) if host is not None else None
        self.headers = Headers(headers or {})
        self.state = state if state is not None else _FakeState()
        self.app = _FakeApp(auth_manager)


@pytest.fixture(autouse=True)
def _auth_enabled(monkeypatch):
    """Pin AUTH_ENABLED to the (unset -> "true") default for every case here
    so ambient environment/test-order pollution can't flip the unrelated
    "auth explicitly disabled" early-return branch."""
    monkeypatch.delenv("AUTH_ENABLED", raising=False)


def test_flag_plus_loopback_grants_admin_no_forwarding_headers():
    """D-05: a request carrying the middleware-only flag from a genuine
    loopback peer with no forwarding headers is granted -- no HTTPException."""
    req = _FakeRequest(
        host="127.0.0.1",
        state=_FakeState(current_user="internal-tool", is_internal_tool=True),
    )
    assert require_admin(req) is None


def test_spoofed_current_user_string_without_flag_is_denied():
    """D-05 (the core SEC-26 fix): current_user == 'internal-tool' on
    request.state, WITHOUT the middleware-only flag, must be DENIED -- the
    string is no longer a grant signal. This is the case that FAILS against
    the pre-fix require_admin (RED)."""
    req = _FakeRequest(
        host="127.0.0.1",
        state=_FakeState(current_user="internal-tool", is_internal_tool=False),
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(req)
    assert exc.value.status_code == 403


def test_spoofed_current_user_string_flag_attribute_absent_is_denied():
    """Same denial as above but the flag attribute is entirely absent from
    request.state (getattr default), not merely False -- the realistic shape
    of a request.state that never passed through AuthMiddleware's
    internal-tool branch at all."""
    req = _FakeRequest(
        host="127.0.0.1",
        state=_FakeState(current_user="internal-tool"),
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(req)
    assert exc.value.status_code == 403


def test_flag_true_but_spoofed_forwarding_header_denied():
    """D-06: even with the flag set, a forwarding/proxy header on the request
    makes is_trusted_loopback False -- require_admin's re-assertion must deny
    (belt-and-suspenders on top of the flag)."""
    req = _FakeRequest(
        host="127.0.0.1",
        headers={"x-forwarded-for": "1.2.3.4"},
        state=_FakeState(current_user="internal-tool", is_internal_tool=True),
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(req)
    assert exc.value.status_code == 403


def test_header_direct_branch_from_loopback_still_grants():
    """Non-regression: the existing header-direct branch (caller itself sets
    X-Prisma-Internal-Token) is unchanged by this plan."""
    req = _FakeRequest(
        host="127.0.0.1",
        headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
        state=_FakeState(current_user=None),
    )
    assert require_admin(req) is None


def test_header_direct_branch_denied_when_not_loopback():
    """Non-regression: the header-direct branch already re-asserts
    is_trusted_loopback -- a forwarded/non-loopback caller presenting the
    correct token is still denied."""
    req = _FakeRequest(
        host="203.0.113.7",
        headers={INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN},
        state=_FakeState(current_user=None),
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(req)
    assert exc.value.status_code == 403
