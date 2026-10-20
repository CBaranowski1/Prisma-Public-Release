"""Tests for the companion pairing endpoints (split 3/4).

Covers what the review asked for:
  - a non-admin / bearer caller cannot call /api/companion/pair (admin-only)
  - the pairing token is minted once (hashed at rest) and the mint invalidates
    the auth cache so it works immediately, no restart
  - minting is a POST, never a GET (CSRF: a SameSite=Lax cookie rides a
    top-level GET, so GET-minting would be triggerable by a link / <img>)
"""

import contextlib
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Capture what mint_token would persist, via a stubbed core.database.
_CAPTURED = {}


class _ApiToken:
    def __init__(self, **kw):
        _CAPTURED.clear()
        _CAPTURED.update(kw)
        self.__dict__.update(kw)


@contextlib.contextmanager
def _get_db_session():
    yield MagicMock()


# core/__init__ pulls in models/session_manager which import many ORM names from
# core.database; under conftest's sqlalchemy stubs the real module can't load.
# A __getattr__ module resolves any non-dunder name to a MagicMock, while keeping
# our real get_db_session/ApiToken for the mint test. Dunder names (e.g. __all__)
# are NOT auto-resolved — the next test file does `from core.database import *`,
# which would otherwise see a MagicMock where a list-of-str is required.
class _DBStub(types.ModuleType):
    def __getattr__(self, name):  # noqa: D401
        if name.startswith("__"):
            raise AttributeError(name)
        return MagicMock()


_db = _DBStub("core.database")
_db.get_db_session = _get_db_session
_db.ApiToken = _ApiToken


@pytest.fixture(autouse=True)
def _companion_pairing_stubs(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", _db)
    for _name, _attrs in {
        "core.auth": {"AuthManager": MagicMock()},
        "src.endpoint_resolver": {"build_chat_url": (lambda u: u)},
    }.items():
        if _name not in sys.modules:
            _mm = types.ModuleType(_name)
            for _k, _v in _attrs.items():
                setattr(_mm, _k, _v)
            sys.modules[_name] = _mm
        monkeypatch.setitem(sys.modules, _name, sys.modules[_name])


from fastapi import HTTPException  # noqa: E402

import companion.pairing as P  # noqa: E402
import companion.routes as R  # noqa: E402
from companion.routes import mint_pairing_token, setup_companion_routes  # noqa: E402
from core.middleware import require_admin  # noqa: E402


# --- token minting: shown once, hashed at rest -----------------------------

def test_mint_token_returns_raw_once_and_stores_only_a_hash(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", _db)
    parent = sys.modules.get("core")
    if parent is not None:
        monkeypatch.setattr(parent, "database", _db, raising=False)

    token_id, raw = P.mint_token("alice")
    assert raw.startswith("ody_")
    # The persisted row stores a bcrypt hash + prefix, never the plaintext.
    assert _CAPTURED["token_hash"] != raw
    assert _CAPTURED["token_hash"].startswith("$2")  # bcrypt
    assert _CAPTURED["token_prefix"] == raw[:8]
    assert _CAPTURED["owner"] == "alice"
    assert _CAPTURED["scopes"] == "chat"
    assert _CAPTURED["is_active"] is True


def test_mint_pairing_token_mints_a_code_not_a_bearer_token(monkeypatch):
    # SEC-22 (D-01): mint_pairing_token now mints a redeemable CODE via
    # mint_pairing_code, never a durable bearer token via mint_token.
    monkeypatch.setattr(P, "mint_pairing_code", lambda owner: "pcd_demo")
    assert mint_pairing_token("alice") == "pcd_demo"


def test_mint_pairing_token_does_not_invalidate_cache_at_mint_time(monkeypatch):
    # A pairing code is not an ApiToken -- the bearer-token cache has nothing
    # to invalidate until redemption mints the durable token (pair_redeem).
    monkeypatch.setattr(P, "mint_pairing_code", lambda owner: "pcd_demo")
    invalidate = MagicMock()
    assert mint_pairing_token("alice", invalidate) == "pcd_demo"
    invalidate.assert_not_called()


def test_mint_pairing_token_tolerates_no_invalidator(monkeypatch):
    monkeypatch.setattr(P, "mint_pairing_code", lambda owner: "pcd_demo")
    # Must not blow up if the app didn't expose an invalidator.
    assert mint_pairing_token("alice", None) == "pcd_demo"


def test_pairing_payload_shape():
    p = P.pairing_payload("192.168.1.9", 7000, "pcd_x")
    assert p == {"v": 2, "host": "192.168.1.9", "port": 7000, "code": "pcd_x"}


# --- admin-only gate: a bearer/non-admin caller is rejected ----------------

def _admin_mgr(is_admin):
    return SimpleNamespace(is_admin=lambda u: is_admin, is_configured=True)


def _req(current_user, *, api_token=False, is_admin=False):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=current_user, api_token=api_token),
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_admin_mgr(is_admin))),
    )


# --- CSRF double-submit helper (SEC-23 D-04) --------------------------------

def test_csrf_tokens_match_rejects_both_none():
    assert P.csrf_tokens_match(None, None) is False


def test_csrf_tokens_match_rejects_both_empty_strings():
    # The empty-string pre-check: secrets.compare_digest("", "") is True, which
    # would wrongly pass a request with neither a cookie nor a submitted value
    # (RESEARCH Pitfall 4).
    assert P.csrf_tokens_match("", "") is False


def test_csrf_tokens_match_rejects_missing_submitted_value():
    assert P.csrf_tokens_match("cookie-value", None) is False
    assert P.csrf_tokens_match("cookie-value", "") is False


def test_csrf_tokens_match_rejects_mismatch():
    assert P.csrf_tokens_match("a", "b") is False


def test_csrf_tokens_match_accepts_equal_values():
    assert P.csrf_tokens_match("abc", "abc") is True


def test_bearer_token_caller_cannot_pair(monkeypatch):
    # Bearer callers come through as the "api" pseudo-user, which is not admin.
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as exc:
        require_admin(_req("api", api_token=True, is_admin=False))
    assert exc.value.status_code == 403


def test_non_admin_user_cannot_pair(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    with pytest.raises(HTTPException) as exc:
        require_admin(_req("bob", is_admin=False))
    assert exc.value.status_code == 403


def test_admin_user_passes_the_gate(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    # Should not raise.
    require_admin(_req("alice", is_admin=True))


# --- CSRF: minting is POST, never GET --------------------------------------

def _pair_methods():
    router = setup_companion_routes()
    methods = set()
    for r in router.routes:
        path = getattr(r, "path", "")
        if path.endswith("/pair"):
            methods |= set(getattr(r, "methods", set()) or set())
    return methods


def _pair_route(method):
    for route in setup_companion_routes().routes:
        path = getattr(route, "path", "")
        if path.endswith("/pair") and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} /api/companion/pair route not found")


def _fake_pair_request(format=None, port=7000, cookies=None, headers=None, client_host="127.0.0.1"):
    """Fake POST/GET /pair request shim.

    Defaults to a trusted-loopback client host (127.0.0.1, no proxy-forwarding
    headers) so pre-existing tests that don't care about D-05 aren't blocked
    by the insecure-channel gate. Tests exercising D-04 (CSRF) or D-05
    (loopback/secure-channel) override `cookies`/`headers`/`client_host`.
    """
    query_params = {}
    if format is not None:
        query_params["format"] = format
    return SimpleNamespace(
        state=SimpleNamespace(current_user="alice", api_token=False),
        headers=headers or {},
        cookies=cookies or {},
        client=SimpleNamespace(host=client_host),
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=_admin_mgr(True),
                invalidate_token_cache=MagicMock(),
            )
        ),
        query_params=query_params,
        url=SimpleNamespace(port=port),
    )


def _csrf_pair(value="csrf-token-abc"):
    """A matching CSRF cookie + header pair for the POST /pair double-submit
    gate (SEC-23 D-04)."""
    return {
        "cookies": {P.CSRF_COOKIE_NAME: value},
        "headers": {"X-CSRF-Token": value},
    }


def test_pair_is_minted_via_post_not_get():
    methods = _pair_methods()
    assert "POST" in methods, "pairing must accept POST (the mint)"
    assert "GET" in methods, "GET should render the form page"
    # The distinction is enforced in the handlers: GET renders a form and never
    # mints; only POST calls mint_pairing_token.


def test_pair_page_uses_imported_admin_gate(monkeypatch):
    monkeypatch.setattr(R, "require_admin", lambda request: None)
    response = _pair_route("GET")(SimpleNamespace())

    assert "Pair a device" in str(getattr(response, "body", response))


def test_pair_get_renders_form_without_minting(monkeypatch):
    mint = MagicMock(side_effect=AssertionError("GET must not mint a token"))
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "mint_pairing_token", mint)

    response = _pair_route("GET")(_fake_pair_request())
    body = response.body.decode()

    assert response.media_type == "text/html"
    assert '<form method="POST" action="/api/companion/pair">' in body
    assert "Generate pairing code" in body
    mint.assert_not_called()


def test_pair_post_json_returns_pairing_payload(monkeypatch):
    mint = MagicMock(return_value="pcd_raw")
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(R, "mint_pairing_token", mint)
    monkeypatch.setattr(R._pairing, "lan_ip_candidates", lambda: ["192.168.1.50"])

    request = _fake_pair_request(format="json", port=7000, **_csrf_pair())
    response = _pair_route("POST")(request)

    mint.assert_called_once_with("alice", request.app.state.invalidate_token_cache)
    assert response["host"] == "192.168.1.50"
    assert response["port"] == 7000
    assert response["code"] == "pcd_raw"
    assert response["payload"] == {
        "v": 2,
        "host": "192.168.1.50",
        "port": 7000,
        "code": "pcd_raw",
    }
    assert "token" not in response
    assert "token_id" not in response
    for secret_key in ("token_hash", "token_prefix", "scopes", "is_active", "owner", "name"):
        assert secret_key not in response
        assert secret_key not in response["payload"]


def test_pair_post_json_qr_failure_returns_null_qr(monkeypatch):
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(R, "mint_pairing_token", lambda owner, invalidate: "pcd_raw")
    monkeypatch.setattr(R._pairing, "lan_ip_candidates", lambda: ["192.168.1.50"])
    monkeypatch.setattr(R._pairing, "pairing_qr_png_data_uri", lambda payload: None)

    request = _fake_pair_request(format="json", port=7000, **_csrf_pair())
    response = _pair_route("POST")(request)

    assert response["qr"] is None
    assert response["host"] == "192.168.1.50"
    assert response["port"] == 7000
    assert response["code"] == "pcd_raw"
    assert response["payload"] == {
        "v": 2,
        "host": "192.168.1.50",
        "port": 7000,
        "code": "pcd_raw",
    }


def test_pair_post_html_escapes_pairing_values(monkeypatch):
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(R, "mint_pairing_token", lambda owner, invalidate: "pcd_<raw>&")
    monkeypatch.setattr(R._pairing, "lan_ip_candidates", lambda: ["host<one>&"])
    monkeypatch.setattr(R._pairing, "pairing_qr_png_data_uri", lambda payload: None)

    request = _fake_pair_request(**_csrf_pair())
    response = _pair_route("POST")(request)
    body = response.body.decode()

    assert response.media_type == "text/html"
    assert "host<one>&" not in body
    assert "pcd_<raw>&" not in body
    assert "host&lt;one&gt;&amp;" in body
    assert "pcd_&lt;raw&gt;&amp;" in body


# --- CSRF double-submit gate on POST /pair (SEC-23 D-04) --------------------

def test_pair_post_missing_csrf_field_is_403(monkeypatch):
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    # Cookie is set (a prior GET /pair issued it) but nothing was submitted --
    # no header, and the caller passes csrf_token=None explicitly (mirrors a
    # form POST with the hidden field stripped/absent).
    request = _fake_pair_request(cookies={P.CSRF_COOKIE_NAME: "cookie-value"})
    with pytest.raises(HTTPException) as exc:
        _pair_route("POST")(request, csrf_token=None)
    assert exc.value.status_code == 403


def test_pair_post_mismatched_csrf_is_403(monkeypatch):
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    request = _fake_pair_request(
        cookies={P.CSRF_COOKIE_NAME: "cookie-value"},
        headers={"X-CSRF-Token": "a-different-value"},
    )
    with pytest.raises(HTTPException) as exc:
        _pair_route("POST")(request)
    assert exc.value.status_code == 403


def test_pair_post_matching_csrf_passes_gate(monkeypatch):
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(R, "mint_pairing_token", lambda owner, invalidate: "pcd_raw")
    monkeypatch.setattr(R._pairing, "lan_ip_candidates", lambda: ["192.168.1.50"])

    request = _fake_pair_request(format="json", **_csrf_pair("matching-value"))
    response = _pair_route("POST")(request)

    assert response["code"] == "pcd_raw"


def test_pair_post_matching_csrf_via_form_field_passes_gate(monkeypatch):
    # The non-JSON (server-rendered <form>) caller submits the hidden field,
    # not the X-CSRF-Token header -- exercise that carrier explicitly.
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(R, "mint_pairing_token", lambda owner, invalidate: "pcd_raw")
    monkeypatch.setattr(R._pairing, "lan_ip_candidates", lambda: ["192.168.1.50"])

    request = _fake_pair_request(cookies={P.CSRF_COOKIE_NAME: "form-field-value"})
    response = _pair_route("POST")(request, csrf_token="form-field-value")

    assert "pcd_raw" in response.body.decode()


# --- GET /pair issues the CSRF cookie + hidden field (SEC-23 D-04) ----------

def test_pair_get_issues_csrf_cookie_and_matching_hidden_field(monkeypatch):
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)

    response = _pair_route("GET")(_fake_pair_request())
    body = response.body.decode()

    set_cookie_header = response.headers.get("set-cookie", "")
    assert P.CSRF_COOKIE_NAME in set_cookie_header, "GET /pair must set the CSRF cookie"

    import re
    match = re.search(rf"{re.escape(P.CSRF_COOKIE_NAME)}=([^;]+)", set_cookie_header)
    assert match, "could not extract CSRF cookie value from Set-Cookie header"
    csrf_value = match.group(1)

    assert 'name="csrf_token"' in body
    assert f'value="{csrf_value}"' in body
