"""Tests for the SEC-22 pairing-code store (companion/pairing.py) and the
POST /api/companion/pair/redeem route (companion/routes.py).

Covers the pending-code store half (mint/redeem/single-use/expiry/prune) --
modeled on routes/copilot_routes.py's `_PENDING`/`_prune_expired` device-code
pending map. The store is pure in-memory, no DB dependency, so no core.database
stub is needed for the store tests (unlike tests/test_companion_pairing.py's
mint_token tests).

The route-level tests at the bottom resolve the real POST /pair/redeem handler
(same `_pair_route`-style resolution idiom as tests/test_companion_pairing.py)
and stub mint_companion_token so no real DB write happens.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import companion.pairing as P  # noqa: E402
import companion.routes as R  # noqa: E402
from companion.routes import setup_companion_routes  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_pending_codes():
    """Isolate each test from the module-level pending-code map."""
    P._PENDING_CODES.clear()
    yield
    P._PENDING_CODES.clear()


# --- mint: hashed at rest, never plaintext ---------------------------------

def test_mint_pairing_code_returns_raw_code_with_prefix():
    raw_code = P.mint_pairing_code("alice")
    assert raw_code.startswith("pcd_")


def test_mint_pairing_code_stores_only_a_bcrypt_hash():
    raw_code = P.mint_pairing_code("alice")
    entry = P._PENDING_CODES[raw_code[:8]]
    assert entry["code_hash"] != raw_code
    assert entry["code_hash"].startswith("$2")  # bcrypt
    assert entry["owner"] == "alice"
    assert "expires_at" in entry


# --- redeem: happy path -----------------------------------------------------

def test_redeem_pairing_code_returns_owner_first_time():
    raw_code = P.mint_pairing_code("alice")
    assert P.redeem_pairing_code(raw_code) == "alice"


# --- single-use --------------------------------------------------------------

def test_redeem_pairing_code_single_redemption():
    raw_code = P.mint_pairing_code("alice")
    assert P.redeem_pairing_code(raw_code) == "alice"
    # Second redemption of the same code must fail -- single-use (SEC-22 core).
    assert P.redeem_pairing_code(raw_code) is None


# --- expiry ------------------------------------------------------------------

def test_redeem_pairing_code_expired_code_returns_none():
    raw_code = P.mint_pairing_code("alice")
    entry = P._PENDING_CODES[raw_code[:8]]
    entry["expires_at"] = 0  # force it into the past
    assert P.redeem_pairing_code(raw_code) is None


# --- unknown / empty / wrong (fail-closed, non-oracle) ------------------------

def test_redeem_pairing_code_empty_code_returns_none():
    assert P.redeem_pairing_code("") is None


def test_redeem_pairing_code_wrong_code_returns_none():
    P.mint_pairing_code("alice")
    assert P.redeem_pairing_code("pcd_wrong") is None


def test_redeem_pairing_code_unknown_code_returns_none():
    assert P.redeem_pairing_code("pcd_" + "z" * 32) is None


# --- prune ---------------------------------------------------------------------

def test_prune_expired_codes_removes_expired_entries():
    raw_code = P.mint_pairing_code("alice")
    prefix = raw_code[:8]
    assert prefix in P._PENDING_CODES
    P._PENDING_CODES[prefix]["expires_at"] = 0
    P._prune_expired_codes()
    assert prefix not in P._PENDING_CODES


# --- POST /api/companion/pair/redeem route wiring ---------------------------
# Resolves the real registered handler (mirrors tests/test_companion_pairing.py's
# _pair_route idiom) so a route-shape regression (wrong path/method) is caught,
# not just the underlying store function.

def _redeem_route():
    for route in setup_companion_routes().routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path.endswith("/pair/redeem") and "POST" in methods:
            return route.endpoint
    raise AssertionError("POST /api/companion/pair/redeem route not found")


def _fake_redeem_request(client_host="127.0.0.1", headers=None):
    """Defaults to a trusted-loopback client (no proxy-forwarding headers) so
    pre-existing redeem tests aren't blocked by the D-05 insecure-channel
    gate. D-05 tests below override client_host/headers explicitly."""
    return SimpleNamespace(
        client=SimpleNamespace(host=client_host),
        headers=headers or {},
        app=SimpleNamespace(state=SimpleNamespace(invalidate_token_cache=MagicMock())),
    )


def test_redeem_route_valid_code_returns_token_and_owner(monkeypatch):
    monkeypatch.setattr(R._pairing, "mint_companion_token", lambda owner: "ody_new")
    raw_code = P.mint_pairing_code("alice")

    request = _fake_redeem_request()
    response = _redeem_route()(request, {"code": raw_code})

    assert response == {"token": "ody_new", "owner": "alice"}
    request.app.state.invalidate_token_cache.assert_called_once()


def test_redeem_route_replay_of_same_code_is_401(monkeypatch):
    monkeypatch.setattr(R._pairing, "mint_companion_token", lambda owner: "ody_new")
    raw_code = P.mint_pairing_code("alice")
    request = _fake_redeem_request()

    _redeem_route()(request, {"code": raw_code})  # first redemption succeeds

    with pytest.raises(HTTPException) as exc:
        _redeem_route()(_fake_redeem_request(), {"code": raw_code})
    assert exc.value.status_code == 401


def test_redeem_route_unknown_code_is_401(monkeypatch):
    monkeypatch.setattr(R._pairing, "mint_companion_token", lambda owner: "ody_new")

    with pytest.raises(HTTPException) as exc:
        _redeem_route()(_fake_redeem_request(), {"code": "pcd_" + "z" * 32})
    assert exc.value.status_code == 401


# --- AUTH_EXEMPT_EXACT wiring (Task 3) --------------------------------------
# /pair/redeem is called by a cookie-less, token-less device -- without this
# exemption AuthMiddleware 401s the request before pair_redeem's own
# fail-closed check ever runs (RESEARCH Pitfall 2). Pin the exemption by
# reading app.py's source directly, mirroring
# tests/test_webhook_trigger_auth_exempt.py's extraction idiom -- importing
# app.py here would pull in the full application stack.

def _read_app_source() -> str:
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app.py",
    )
    with open(app_path, encoding="utf-8") as fh:
        return fh.read()


def test_pair_redeem_path_is_in_auth_exempt_exact():
    src = _read_app_source()
    start = src.find("AUTH_EXEMPT_EXACT")
    assert start != -1, "AUTH_EXEMPT_EXACT not declared in app.py"
    lb = src.find("{", start)
    assert lb != -1
    rb = src.find("}", lb)
    assert rb != -1
    body = src[lb + 1:rb]
    assert '"/api/companion/pair/redeem"' in body, (
        "/api/companion/pair/redeem must be in AUTH_EXEMPT_EXACT (SEC-22) -- "
        "without it AuthMiddleware 401s the redeem request before the "
        "handler's own code check ever runs"
    )


# --- D-05: refuse non-loopback pairing/redemption over an insecure channel --
# (SEC-23 D-05) -- applies to BOTH POST /pair (mint) and POST /pair/redeem.

def _admin_mgr(is_admin=True):
    return SimpleNamespace(is_admin=lambda u: is_admin, is_configured=True)


def _pair_create_route():
    for route in setup_companion_routes().routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path.endswith("/pair") and "POST" in methods:
            return route.endpoint
    raise AssertionError("POST /api/companion/pair route not found")


def _fake_mint_request(client_host="127.0.0.1", headers=None, cookies=None, port=7000):
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
        query_params={"format": "json"},
        url=SimpleNamespace(port=port),
    )


def _matching_csrf(value="d05-csrf-value"):
    return {"cookies": {R._pairing.CSRF_COOKIE_NAME: value}, "headers": {"X-CSRF-Token": value}}


def test_pair_create_refuses_non_loopback_insecure_channel(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")

    request = _fake_mint_request(client_host="203.0.113.5", **_matching_csrf())
    with pytest.raises(HTTPException) as exc:
        _pair_create_route()(request)
    assert exc.value.status_code == 403


def test_pair_create_refuses_proxy_forwarded_insecure_channel(monkeypatch):
    # A loopback client.host behind a proxy/tunnel forwarding header is NOT
    # trusted-loopback (is_trusted_loopback's Cloudflare-tunnel guard) --
    # confirm D-05 refuses this too, not just a bare non-loopback host.
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")

    pair = _matching_csrf()
    pair["headers"]["x-forwarded-for"] = "203.0.113.5"
    request = _fake_mint_request(client_host="127.0.0.1", **pair)
    with pytest.raises(HTTPException) as exc:
        _pair_create_route()(request)
    assert exc.value.status_code == 403


def test_pair_redeem_refuses_non_loopback_insecure_channel(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setattr(R._pairing, "mint_companion_token", lambda owner: "ody_new")
    raw_code = P.mint_pairing_code("alice")

    request = _fake_redeem_request(client_host="203.0.113.5")
    with pytest.raises(HTTPException) as exc:
        _redeem_route()(request, {"code": raw_code})
    assert exc.value.status_code == 403


def test_pair_create_trusted_loopback_not_refused_on_channel_grounds(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(R, "mint_pairing_token", lambda owner, invalidate: "pcd_raw")
    monkeypatch.setattr(R._pairing, "lan_ip_candidates", lambda: ["192.168.1.50"])

    request = _fake_mint_request(client_host="127.0.0.1", **_matching_csrf())
    response = _pair_create_route()(request)  # must not raise for channel reasons
    assert response["code"] == "pcd_raw"


def test_pair_create_non_loopback_with_secure_cookies_not_refused(monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    monkeypatch.setattr(R, "require_admin", lambda request: None, raising=False)
    monkeypatch.setattr(R, "get_current_user", lambda request: "alice")
    monkeypatch.setattr(R, "mint_pairing_token", lambda owner, invalidate: "pcd_raw")
    monkeypatch.setattr(R._pairing, "lan_ip_candidates", lambda: ["192.168.1.50"])

    request = _fake_mint_request(client_host="203.0.113.5", **_matching_csrf())
    response = _pair_create_route()(request)  # must not raise for channel reasons
    assert response["code"] == "pcd_raw"


def test_pair_redeem_trusted_loopback_not_refused_on_channel_grounds(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)
    monkeypatch.setattr(R._pairing, "mint_companion_token", lambda owner: "ody_new")
    raw_code = P.mint_pairing_code("alice")

    request = _fake_redeem_request(client_host="127.0.0.1")
    response = _redeem_route()(request, {"code": raw_code})
    assert response == {"token": "ody_new", "owner": "alice"}


def test_pair_redeem_non_loopback_with_secure_cookies_not_refused(monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    monkeypatch.setattr(R._pairing, "mint_companion_token", lambda owner: "ody_new")
    raw_code = P.mint_pairing_code("alice")

    request = _fake_redeem_request(client_host="203.0.113.5")
    response = _redeem_route()(request, {"code": raw_code})
    assert response == {"token": "ody_new", "owner": "alice"}

