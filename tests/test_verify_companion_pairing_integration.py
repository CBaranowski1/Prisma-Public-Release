"""SC-4 (D-07): the assembled-app integration proof for Phase 16's device-
pairing handshake -- the named "integration-checker" artifact honoring this
project's "presence != integration" principle.

Every assertion in this file is driven through the REAL, composed FastAPI
`app` object via `starlette.testclient.TestClient`, past the real
`AuthMiddleware`, never by calling `companion.pairing`/`companion.routes`
handler functions directly. That distinction is load-bearing here
specifically: `/api/companion/pair/redeem` is the one route in this app that
is AUTH_EXEMPT_EXACT ("the path is the credential" -- 16-03-SUMMARY.md). A
test that only unit-tests `redeem_pairing_code()` in isolation, or calls the
route function directly, cannot catch a wrong/missing AUTH_EXEMPT_EXACT entry
-- AuthMiddleware would 401 the request before the handler's own code ever
runs, and a direct-call test would never observe that (16-RESEARCH.md
Pitfall 2). This file proves reachability THROUGH the middleware first
(redeem succeeds), then proves the security invariants on top of that
(replay denied, expiry enforced, CSRF enforced, insecure-channel refused).

Reuses the `_heal_polluted_modules` / `app_module_instance` / `_patch_auth`
assembled-app harness from `tests/test_verify_qb_privilege.py:114-190`
VERBATIM (14-/15-/16-PATTERNS.md precedent) -- extended only so the fake
auth_manager's `is_admin` returns True for the pairing admin (POST /pair is
admin-gated; `test_verify_qb_privilege.py`'s original shim hardcodes
`is_admin=False`, which would 403 every mint in this file).

Covers, end to end through the real app:
  - T-16-01 (SEC-22): a redeemed pairing code cannot be redeemed again --
    replay of the same code returns 401.
  - T-16-03 (SEC-22): a durable ApiToken past its expires_at is rejected
    (401) as a Bearer credential through the real AuthMiddleware bearer path.
  - T-16-02 (SEC-23): a cross-site POST /pair without a matching CSRF token
    is refused (403), including the missing-cookie-and-field case (Pitfall 4
    -- the exact shape a real cross-site forger would send, since they
    cannot read the victim's cookie to copy it into a field).
  - T-16-06 (SEC-23 D-05): a non-loopback pairing attempt over a non-Secure
    channel is refused (403) on BOTH POST /pair and POST /pair/redeem.
  - Reachability: /pair/redeem's AUTH_EXEMPT_EXACT membership is proven by
    the redeem call actually succeeding through the real middleware stack,
    not by a source-scan of the exemption set.

All DB writes in this file go through the app's own live
`core.database.SessionLocal`/`ApiToken` (same objects app.py imports at
module scope) -- there is no isolated test database for this suite. Every
row this file creates is scoped to a distinctive test-only `owner` value and
deleted in a `finally` block, so no SC-4 proof run leaves durable rows behind
in the real app database.
"""
import html
import importlib.util
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Assembled-app harness (reused verbatim from tests/test_verify_qb_privilege.py
# L114-190 -- see that file for the full rationale on each step; kept inline
# here per that file's own note that importing a fixture across test modules
# is not reliable).
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
    # tests/test_companion_pairing.py's autouse fixture raw-inserts synthetic
    # stub modules (core.database / core.auth / src.endpoint_resolver) into
    # sys.modules and leaks them (its monkeypatch.setitem records the stub it
    # just inserted as the restore baseline, so teardown re-installs the stub
    # rather than deleting it). Those stubs expose only a partial surface
    # (e.g. src.endpoint_resolver has build_chat_url but not resolve_endpoint),
    # so a later `import app` -> routes.memory_routes raises ImportError under
    # that collection order. They are NOT MagicMock-typed, so the loop above
    # misses them; evict any synthetic (no on-disk origin) copy so the real
    # module re-imports and the assembled app builds regardless of test order.
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
    AuthMiddleware (and hence AUTH_EXEMPT_EXACT / require_admin / the bearer
    path) is on the request path instead of bypassed -- otherwise every gate
    this file proves would be a no-op."""
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    # Environment gap (DEBT-03, out of Phase-16 scope): bs4 is a documented
    # dependency (requirements.txt) but not installed in this venv. Stub it
    # ONLY for the duration of building the assembled app here, and ONLY if
    # genuinely absent, then remove the stub immediately after.
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


def _fake_auth_manager(*, user):
    """Test-only auth shim (T-09-03): a stand-in auth_manager object, NOT a
    modification of shipped source. Extended from test_verify_qb_privilege.py's
    original (which hardcodes is_admin=False) so `is_admin` returns True for
    the pairing admin -- POST /api/companion/pair is admin-gated
    (require_admin), unlike that file's datasource routes."""
    return SimpleNamespace(
        is_configured=True,
        validate_token=lambda token: True,
        get_username_for_token=lambda token: user,
        get_privileges=lambda u: {},
        is_admin=lambda u: True,
    )


def _patch_auth(monkeypatch, app_module_instance, *, user):
    """Monkeypatch the fake auth_manager onto BOTH the module-global
    `auth_manager` app.py's AuthMiddleware closes over, and
    `app.state.auth_manager` that require_admin reads -- both must agree so
    the middleware authenticates the TestClient call the same way
    require_admin sees it. monkeypatch reverts both at test teardown."""
    fake_auth = _fake_auth_manager(user=user)
    monkeypatch.setattr(app_module_instance, "auth_manager", fake_auth)
    monkeypatch.setattr(app_module_instance.app.state, "auth_manager", fake_auth)
    return fake_auth


# ---------------------------------------------------------------------------
# File-local helpers -- confined to this test file (T-09-03), never shipped.
# ---------------------------------------------------------------------------

ADMIN_USER = "sc4-pairing-admin"
_SESSION_COOKIE_VALUE = "sc4-faketoken"


def _admin_client(app_module_instance, *, client_host=("127.0.0.1", 51000)):
    """A TestClient authenticated as the fake admin (via the patched
    auth_manager's validate_token/get_username_for_token), on a genuinely
    trusted-loopback client host so D-05 (SEC-23) does not interfere with
    tests that are proving something else."""
    from routes.auth_routes import SESSION_COOKIE

    return TestClient(
        app_module_instance.app,
        client=client_host,
        cookies={SESSION_COOKIE: _SESSION_COOKIE_VALUE},
    )


def _extract_csrf(html_text: str) -> str:
    """Pull the hidden csrf_token form-field value out of GET /pair's
    rendered HTML (companion/routes.py's pair_page)."""
    m = re.search(r'name="csrf_token"\s+value="([^"]*)"', html_text)
    assert m, "csrf_token hidden field not found in GET /pair response HTML"
    return html.unescape(m.group(1))


def _cleanup_api_tokens(owner: str):
    """Delete every ApiToken row this file minted for `owner`, via the app's
    own live SessionLocal, so the SC-4 proof never leaves durable rows behind
    in the real app database."""
    from core.database import ApiToken, SessionLocal

    db = SessionLocal()
    try:
        db.query(ApiToken).filter(ApiToken.owner == owner).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task 1: happy-path mint -> redeem -> use -> replay-denied -> expired-401.
# ---------------------------------------------------------------------------

def test_sc4_mint_redeem_use_replay_denied_and_expired_rejected(app_module_instance, monkeypatch):
    """SC-4 (D-07) core lifecycle, driven entirely through the assembled app:

    1. GET /pair issues the CSRF cookie + matching hidden field.
    2. POST /pair (matching CSRF) mints a CODE -- never a bearer token.
    3. POST /pair/redeem exchanges the code for a durable token. This is the
       reachability proof: it succeeds ONLY because
       /api/companion/pair/redeem is genuinely a member of app.py's
       AUTH_EXEMPT_EXACT and reachable through the real AuthMiddleware.dispatch
       (16-RESEARCH.md Pitfall 2) -- a missing/misspelled exemption entry
       would 401 this call before pair_redeem's own code ever runs.
    4. Replaying the SAME code is denied (401) -- single-use (T-16-01).
    5. The durable token authenticates a real Bearer request (mint->redeem->
       use end-to-end).
    6. Rewinding that token's expires_at into the past (via the app's own
       live SessionLocal) and invalidating the cache makes the SAME token
       401 on the next request -- both-sites expiry enforcement (T-16-03),
       proven through the real bearer-auth path, not a unit-level selection
       check.
    """
    _patch_auth(monkeypatch, app_module_instance, user=ADMIN_USER)
    client = _admin_client(app_module_instance)

    try:
        # 1. GET /pair -> CSRF cookie + matching hidden field.
        resp_get = client.get("/api/companion/pair")
        assert resp_get.status_code == 200, resp_get.text
        csrf_token = _extract_csrf(resp_get.text)

        from companion.pairing import CSRF_COOKIE_NAME

        assert client.cookies.get(CSRF_COOKIE_NAME) == csrf_token

        # 2. POST /pair with the matching CSRF -> 200; payload carries a
        # `code`, never a usable bearer `ody_` token (SEC-22 D-01 wire shape).
        resp_mint = client.post(
            "/api/companion/pair?format=json",
            data={"csrf_token": csrf_token},
        )
        assert resp_mint.status_code == 200, resp_mint.text
        mint_payload = resp_mint.json()
        code = mint_payload.get("code")
        assert code, "POST /pair must return a redeemable code"
        assert "ody_" not in str(mint_payload), "pairing payload must never carry a bearer token"

        # 3. POST /pair/redeem with the code -> 200, durable token + owner.
        # Reaching here at all proves AUTH_EXEMPT_EXACT reachability.
        resp_redeem = client.post("/api/companion/pair/redeem", json={"code": code})
        assert resp_redeem.status_code == 200, resp_redeem.text
        redeem_payload = resp_redeem.json()
        token = redeem_payload["token"]
        owner = redeem_payload["owner"]
        assert token.startswith("ody_")
        assert owner == ADMIN_USER

        try:
            # 4. Replay the SAME code -> 401 (single-use, SEC-22 core claim).
            resp_replay = client.post("/api/companion/pair/redeem", json={"code": code})
            assert resp_replay.status_code == 401, resp_replay.text

            # 5. Use the durable token as a Bearer credential -> 200
            # (mint -> redeem -> use, end-to-end through the real bearer path).
            resp_ping = client.get(
                "/api/companion/ping",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_ping.status_code == 200, resp_ping.text
            assert resp_ping.json()["auth"] == "token"

            # 6. Expiry: rewind expires_at into the past via the app's own
            # live SessionLocal, invalidate the cache, and assert the SAME
            # token is now rejected (SEC-22 both-sites enforcement, Plan 01).
            from datetime import timedelta

            from core.database import ApiToken, SessionLocal, utcnow_naive

            db = SessionLocal()
            try:
                db.query(ApiToken).filter(ApiToken.owner == owner).update(
                    {"expires_at": utcnow_naive() - timedelta(days=1)}
                )
                db.commit()
            finally:
                db.close()
            app_module_instance.app.state.invalidate_token_cache()

            resp_expired = client.get(
                "/api/companion/ping",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp_expired.status_code == 401, resp_expired.text
        finally:
            _cleanup_api_tokens(owner)
    finally:
        app_module_instance.app.state.invalidate_token_cache()


# ---------------------------------------------------------------------------
# Task 2: refusal proofs -- cross-site CSRF 403 + non-loopback-insecure 403.
# ---------------------------------------------------------------------------

def test_sc4_cross_site_csrf_missing_is_refused(app_module_instance, monkeypatch):
    """A POST /pair with NEITHER a CSRF cookie NOR a submitted value -> 403.

    This is the Pitfall-4 shape most test-writers forget: a genuine
    cross-site forger cannot read the victim's cookie in the first place, so
    their forged request carries no cookie AND no field/header -- not merely
    a *wrong* one. A naive `secrets.compare_digest("", "")` (True) would
    wrongly let exactly this request through; csrf_tokens_match's
    truthiness pre-check must reject it (403)."""
    _patch_auth(monkeypatch, app_module_instance, user=ADMIN_USER)
    client = _admin_client(app_module_instance)

    resp = client.post("/api/companion/pair?format=json", data={})
    assert resp.status_code == 403, resp.text


def test_sc4_cross_site_csrf_mismatched_is_refused(app_module_instance, monkeypatch):
    """A POST /pair with a submitted CSRF value that does NOT match the
    cookie GET /pair issued -> 403 (the classic wrong-token case, distinct
    from the missing-cookie-and-field case above)."""
    _patch_auth(monkeypatch, app_module_instance, user=ADMIN_USER)
    client = _admin_client(app_module_instance)

    resp_get = client.get("/api/companion/pair")
    assert resp_get.status_code == 200, resp_get.text
    _extract_csrf(resp_get.text)  # confirms a genuine cookie+field pair was issued

    resp = client.post(
        "/api/companion/pair?format=json",
        data={"csrf_token": "not-the-cookie-value"},
    )
    assert resp.status_code == 403, resp.text


def test_sc4_non_loopback_insecure_pair_and_redeem_refused(app_module_instance, monkeypatch):
    """A non-loopback pairing attempt over a non-Secure channel is refused
    (403) on BOTH POST /pair (mint) and POST /pair/redeem (SEC-23 D-05).

    The default starlette TestClient host is ("testclient", 50000) -- NOT
    loopback -- and SECURE_COOKIES is explicitly unset here, so this is
    exactly the insecure/non-loopback shape D-05 exists to refuse. D-05 is
    checked as the FIRST line in both handlers (ahead of require_admin/CSRF
    in pair_create), so this 403 fires regardless of admin/CSRF state."""
    _patch_auth(monkeypatch, app_module_instance, user=ADMIN_USER)
    monkeypatch.delenv("SECURE_COOKIES", raising=False)

    from routes.auth_routes import SESSION_COOKIE

    client = TestClient(
        app_module_instance.app,
        cookies={SESSION_COOKIE: _SESSION_COOKIE_VALUE},
    )  # default client host ("testclient", 50000) is NOT trusted-loopback

    resp_mint = client.post("/api/companion/pair?format=json")
    assert resp_mint.status_code == 403, resp_mint.text

    resp_redeem = client.post("/api/companion/pair/redeem", json={"code": "irrelevant"})
    assert resp_redeem.status_code == 403, resp_redeem.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
