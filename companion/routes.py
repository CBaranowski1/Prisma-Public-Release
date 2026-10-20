"""Companion bridge — /api/companion/*.

A thin, additive layer so a LAN client (e.g. a phone) can discover what a server
offers and pair to it, without duplicating any LLM logic.

Auth is enforced globally by AuthMiddleware (app.py), so reaching a handler here
means the caller is authenticated by either a cookie session or a Bearer `ody_`
API token -- with ONE deliberate exception: POST /pair/redeem. That call is
made by a cookie-less, token-less device exchanging a one-time pairing code for
its very first token, so it cannot possibly carry a cookie or bearer credential
yet. It is listed in app.py's AUTH_EXEMPT_EXACT (the pairing code IS the
credential for that one call, mirroring the webhook-token exemption) and the
handler itself validates the code and fails closed (401) -- see SEC-22 (D-01).
Every other route in this file (ping/info/models, and the /pair mint) keeps the
original invariant: reaching the handler means the caller is authenticated. The
read endpoints (ping/info/models) accept either cookie or bearer; /pair (mint)
is admin-cookie only.

Pairing CSRF posture: minting happens ONLY on POST. The session cookie is
SameSite=Lax (routes/auth_routes.py), which a browser does not send on a
cross-site POST, so an admin's cookie can't be used by a malicious page to mint
a code -- the same protection the existing POST /api/tokens relies on. Minting
on a GET would be unsafe (Lax cookies ride top-level GET navigations), so GET
/pair only renders a form. On top of SameSite=Lax, POST /pair also requires an
explicit double-submit CSRF token (SEC-23 D-04): GET /pair issues a random
token as a cookie AND embeds it as a hidden form field; POST /pair rejects
(403) unless the submitted value (hidden field or X-CSRF-Token header)
constant-time-matches the cookie. /pair/redeem needs no CSRF check: its caller
has no ambient cookie for a forger to ride (RESEARCH Pitfall 3).

Insecure-channel posture (SEC-23 D-05): both POST /pair and POST /pair/redeem
refuse (403) a request that is neither trusted-loopback
(core.middleware.is_trusted_loopback) nor over a Secure-cookie channel
(SECURE_COOKIES=true) -- pairing is intrinsically a loopback/LAN action, and a
non-loopback pairing attempt over plaintext risks leaking the session cookie
or the pairing code.
"""

import html
import os

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.middleware import is_trusted_loopback, require_admin
from src.auth_helpers import get_current_user

from companion import pairing as _pairing


def token_owner(request: Request) -> str | None:
    """The real owner to attribute a request to, for read-scoping.

    Cookie sessions resolve to the logged-in username via get_current_user.
    Bearer-token callers come through as the sandboxed pseudo-user "api"; their
    real owner is stamped on request.state.api_token_owner by the auth
    middleware. Returns None when no owner can be resolved.
    """
    if getattr(request.state, "api_token", False):
        return getattr(request.state, "api_token_owner", None)
    return get_current_user(request)


def owner_can_see(row_owner, owner) -> bool:
    """Owner-scope rule for read endpoints.

    A caller sees a row when it is their own, or when it is a legacy null-owner
    ("shared") row. A caller must NEVER see another owner's row. Mirrors the
    `owner_filter` rule used elsewhere, expressed as a pure predicate so it can
    be tested directly and used as a defensive in-Python check alongside the
    SQL filter.
    """
    return row_owner is None or row_owner == owner


def mint_pairing_token(owner: str, invalidate=None) -> str:
    """Mint a short-lived, single-use pairing CODE (SEC-22 D-01). Returns the
    raw code; the raw code is shown once (never a usable bearer token).

    No token-cache invalidation happens here -- a pairing code is not an
    ApiToken, so the auth middleware's bearer-token cache has nothing to
    invalidate at mint time. The `invalidate` parameter is accepted (and
    unused) purely to keep this call site's shape stable for callers; the
    actual invalidation now happens at redemption time (pair_redeem), when a
    durable ApiToken is minted and must work on the very next request.
    """
    return _pairing.mint_pairing_code(owner)


def setup_companion_routes() -> APIRouter:
    router = APIRouter(prefix="/api/companion", tags=["companion"])

    @router.get("/ping")
    def ping(request: Request):
        """Cheap, auth-validated health check. A 200 with ok=true confirms the
        host/port and credential are valid; middleware returns 401 otherwise."""
        from core.constants import APP_VERSION
        return {
            "ok": True,
            "name": "prisma",
            "version": APP_VERSION,
            "auth": "token" if getattr(request.state, "api_token", False) else "session",
        }

    @router.get("/info")
    def info(request: Request):
        """Server identity + coarse capability flags. `owner` is the caller's own
        identity (the token's owner for bearer callers)."""
        from core.constants import APP_VERSION
        return {
            "name": "prisma",
            "version": APP_VERSION,
            "owner": token_owner(request),
            "capabilities": {"chat": True, "streaming": True},
        }

    @router.get("/models")
    def models(request: Request):
        """LLM model endpoints the CALLER can use.

        The stock /api/models route scopes to get_current_user, which for a
        bearer token is the sandboxed pseudo-user "api" (owns nothing). Here we
        scope to the token's real owner instead, plus legacy null-owner shared
        rows -- the same rule as owner_filter. Read-only; never returns api_key
        material.
        """
        import json as _json

        from core.database import SessionLocal, ModelEndpoint
        from src.endpoint_resolver import build_chat_url

        owner = token_owner(request)
        out = []
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(
                ModelEndpoint.is_enabled == True,  # noqa: E712
                (ModelEndpoint.model_type == "llm") | (ModelEndpoint.model_type == None),  # noqa: E711
            )
            if owner:
                q = q.filter((ModelEndpoint.owner == owner) | (ModelEndpoint.owner == None))  # noqa: E711
            for ep in q.all():
                if not owner_can_see(ep.owner, owner):
                    continue
                try:
                    model_ids = _json.loads(ep.cached_models) if ep.cached_models else []
                except (ValueError, TypeError):
                    model_ids = []
                try:
                    hidden = set(_json.loads(ep.hidden_models)) if ep.hidden_models else set()
                except (ValueError, TypeError):
                    hidden = set()
                model_ids = [m for m in model_ids if m not in hidden]
                try:
                    chat_url = build_chat_url(ep.base_url)
                except Exception:
                    chat_url = ep.base_url
                out.append({
                    "endpoint_id": ep.id,
                    "name": ep.name,
                    "endpoint_url": chat_url,
                    "models": model_ids,
                    "supports_tools": ep.supports_tools,
                })
        finally:
            db.close()
        return {"endpoints": out}

    @router.get("/pair")
    def pair_page(request: Request):
        """Admin-only pairing page. Renders a form that POSTs to mint a code.

        A GET never mints a credential: SameSite=Lax session cookies ride
        top-level GET navigations, so minting on GET would be triggerable by a
        link or <img> (CSRF). The actual mint is the POST handler below.

        Also issues the double-submit CSRF token (SEC-23 D-04): a random value
        set BOTH as a cookie (scoped to /api/companion/pair) and embedded as a
        hidden `csrf_token` form field, so the subsequent POST can prove it
        saw this exact response (a cross-site forger cannot read the cookie
        to copy its value into the field).
        """
        require_admin(request)
        csrf_token = _pairing.new_csrf_token()
        page = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair a device</title>
<style>
  body{font-family:-apple-system,system-ui,sans-serif;max-width:520px;margin:48px auto;padding:0 20px;color:#e8e8e8;background:#16161a}
  .card{background:#1f1f25;border:1px solid #2c2c35;border-radius:14px;padding:28px;text-align:center}
  button{background:#7c9cff;color:#0e0e12;border:none;border-radius:10px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer}
</style></head>
<body><div class="card">
  <h2>Pair a device</h2>
  <p>Generate a one-time pairing code for a LAN client. The device exchanges it for an access token via /api/companion/pair/redeem.</p>
  <form method="POST" action="/api/companion/pair">
    <input type="hidden" name="csrf_token" value="{CSRF_TOKEN}">
    <button type="submit">Generate pairing code</button>
  </form>
  <p style="color:#8a8a96;font-size:12px;margin-top:18px">Admin only. Each code is single-use and short-lived. Manage or revoke redeemed tokens under Settings &rarr; API tokens.</p>
</div></body></html>""".replace("{CSRF_TOKEN}", html.escape(csrf_token))
        response = HTMLResponse(page)
        response.set_cookie(
            key=_pairing.CSRF_COOKIE_NAME,
            value=csrf_token,
            httponly=False,  # the server-rendered <form> above needs to embed it
            samesite="lax",
            secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
            path="/api/companion/pair",
            max_age=_pairing.PAIRING_CODE_TTL_SECONDS,
        )
        return response

    @router.post("/pair")
    def pair_create(request: Request, csrf_token: str | None = Form(None)):
        """Mint a short-lived, single-use pairing CODE (SEC-22 D-01) -- never a
        usable bearer token. Admin-cookie only; CSRF-safe because the
        SameSite=Lax session cookie is not sent on a cross-site POST (same
        protection as POST /api/tokens) PLUS an explicit double-submit CSRF
        token (SEC-23 D-04) -- the submitted value (hidden form field or
        X-CSRF-Token header, for the `?format=json` caller) must match the
        cookie GET /pair issued, or the request is refused (403). Also refuses
        (403) a non-loopback request over an insecure channel (SEC-23 D-05).
        `?format=json` returns the payload for an in-app pairing screen. The
        device must POST the code to /api/companion/pair/redeem to exchange it
        for the durable, bounded-expiry companion token."""
        secure_cookies = os.getenv("SECURE_COOKIES", "false").lower() == "true"
        if not is_trusted_loopback(request) and not secure_cookies:
            raise HTTPException(403, "Pairing refused: non-loopback request over an insecure channel")

        require_admin(request)

        submitted_csrf = request.headers.get("X-CSRF-Token") or csrf_token
        if not _pairing.csrf_tokens_match(request.cookies.get(_pairing.CSRF_COOKIE_NAME), submitted_csrf):
            raise HTTPException(403, "CSRF token missing or mismatched")

        owner = get_current_user(request)
        invalidate = getattr(request.app.state, "invalidate_token_cache", None)
        raw_code = mint_pairing_token(owner, invalidate)

        hosts = _pairing.lan_ip_candidates()
        host = hosts[0] if hosts else "127.0.0.1"
        port = request.url.port or _pairing.default_port()
        payload = _pairing.pairing_payload(host, port, raw_code)
        qr = _pairing.pairing_qr_png_data_uri(payload)
        qr_ok = bool(qr and qr.startswith("data:image/png;base64,"))

        if (request.query_params.get("format") or "").lower() == "json":
            return {
                "host": host,
                "port": port,
                "code": raw_code,
                "hosts": hosts,
                "payload": payload,
                "qr": qr if qr_ok else None,
            }

        import json as _json
        payload_json = _json.dumps(payload, separators=(",", ":"))
        # Only ever emit a known PNG data-URI into the src; every other value is
        # html.escaped.
        qr_block = (
            f'<img src="{html.escape(qr)}" alt="Pairing QR" width="260" height="260">'
            if qr_ok else "<p><em>QR rendering unavailable -- enter the details manually.</em></p>"
        )
        page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pairing code</title>
<style>
  body{{font-family:-apple-system,system-ui,sans-serif;max-width:520px;margin:40px auto;padding:0 20px;color:#e8e8e8;background:#16161a}}
  .card{{background:#1f1f25;border:1px solid #2c2c35;border-radius:14px;padding:24px;text-align:center}}
  code{{background:#0e0e12;padding:2px 6px;border-radius:6px;word-break:break-all}}
  .row{{text-align:left;margin:10px 0;font-size:14px;color:#bdbdc7}}
  .warn{{color:#e0a85e;font-size:13px;margin-top:18px}}
</style></head>
<body><div class="card">
  <h2>Pairing code</h2>
  {qr_block}
  <div class="row"><strong>Host:</strong> <code>{html.escape(host)}</code></div>
  <div class="row"><strong>Port:</strong> <code>{html.escape(str(port))}</code></div>
  <div class="row"><strong>Pairing code:</strong> <code>{html.escape(raw_code)}</code></div>
  <div class="row"><strong>Payload:</strong> <code>{html.escape(payload_json)}</code></div>
  <p class="warn">Shown once. This code is single-use and short-lived; the
  device exchanges it for an access token via /api/companion/pair/redeem. The
  device must be on the same network, and the server must bind to your LAN.</p>
</div></body></html>"""
        return HTMLResponse(page)

    @router.post("/pair/redeem")
    def pair_redeem(request: Request, body: dict):
        """Exchange a valid, unexpired, unredeemed pairing code EXACTLY ONCE for
        the durable companion ApiToken (SEC-22 D-01).

        Deliberately NOT admin-gated and NOT CSRF-checked: this call is made by
        a cookie-less, token-less device -- it has no ambient session cookie
        for a cross-site forger to ride, so there is nothing for CSRF to
        defend (RESEARCH Pitfall 3; CSRF double-submit only applies to
        POST /pair). The pairing code itself is the entire
        defense here, exactly like the webhook-token exemption this route
        mirrors in app.py's AUTH_EXEMPT_EXACT. D-05 (loopback/secure-channel
        refusal) DOES apply here -- checked first, below.

        Any failure (unknown/expired/already-redeemed/wrong code) returns one
        flat 401 -- redeem_pairing_code() already collapses every failure mode
        into a single None so an attacker can't tell "expired" from "wrong"
        from "already used" (non-oracle, fail-closed).
        """
        secure_cookies = os.getenv("SECURE_COOKIES", "false").lower() == "true"
        if not is_trusted_loopback(request) and not secure_cookies:
            raise HTTPException(403, "Redemption refused: non-loopback request over an insecure channel")

        owner = _pairing.redeem_pairing_code((body or {}).get("code", ""))
        if owner is None:
            raise HTTPException(401, "Invalid, expired, or already-redeemed pairing code")

        raw_token = _pairing.mint_companion_token(owner)
        invalidate = getattr(request.app.state, "invalidate_token_cache", None)
        if callable(invalidate):
            invalidate()
        return {"token": raw_token, "owner": owner}

    return router
