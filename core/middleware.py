# src/middleware.py
# Shared middleware, decorators, and request helpers

import os
import secrets

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


# Per-process token that lets the in-app tool layer hit admin-gated
# routes via HTTP loopback (the agent's tool calls don't carry the
# admin user's session cookie). Set once at import; tools read the
# same value from this module. Never persisted or exposed externally.
INTERNAL_TOOL_TOKEN = os.environ.get("PRISMA_INTERNAL_TOKEN") or secrets.token_hex(32)
INTERNAL_TOOL_HEADER = "X-Prisma-Internal-Token"

# SEC-25 (D-01): loopback trust is an ALLOWLIST-BY-ABSENCE, not an enumerated
# proxy-header denylist. A bare ``client.host in ('127.0.0.1','::1')`` check is
# unsafe behind a Cloudflare tunnel / reverse proxy: those connect from
# loopback, so a remote visitor would otherwise inherit local trust and slip
# past LOCALHOST_BYPASS or spoof the internal-tool path. Instead of hand-
# curating a list of known forwarding-header names (which omits any future
# vendor header, e.g. True-Client-IP/X-Client-IP), `_is_forwarding_header`
# detects the ENTIRE family STRUCTURALLY, by shape: `forwarded`/`via`,
# `x-forwarded*`, any `*-forwarded-*`, the `cf-` prefix (covers
# cf-connecting-ip/cf-ray/cf-visitor + any future Cloudflare header --
# `cf-connecting-ip` ends in `-connecting-ip`, NOT `-client-ip`, so a prefix
# rule is required, not a suffix rule), `*-client-ip` (true-client-ip,
# x-client-ip, fastly-client-ip, fly-client-ip, x-cluster-client-ip), and
# `*-real-ip`. Presence of ANY such header on a loopback-peer request means
# it is NOT trusted-loopback (fail closed). Starlette's `Headers` lowercases
# every header name at construction time, so no manual `.lower()` is needed
# here -- `request.headers.keys()` already yields lowercase strings.
def _is_forwarding_header(name: str) -> bool:
    """True if `name` (already lowercase) looks like a proxy/tunnel
    forwarding or client-IP-disclosure header, by SHAPE rather than an
    enumerated list, so a not-yet-invented vendor header still trips it."""
    if name in ("forwarded", "via"):
        return True
    if name.startswith("x-forwarded"):
        return True
    if "-forwarded-" in name:
        return True
    if name.startswith("cf-"):
        return True
    if name.endswith("-client-ip") or name == "client-ip":
        return True
    if name.endswith("-real-ip") or name == "real-ip":
        return True
    return False


def _has_forwarding_header(request: Request) -> bool:
    return any(_is_forwarding_header(h) for h in request.headers.keys())


# SEC-25 (D-02): opt-in escape hatch for a properly-fronted deployment. Unset
# (default) means the strict absence-check above is the ENTIRE loopback
# story -- no proxy in front is trusted by default, matching the default
# loopback/LAN deployment. When set, it NAMES the one forwarded-header the
# operator's own reverse proxy/tunnel is trusted to set correctly, and the
# real client identity is derived from THAT header instead of the raw peer.
#
# Scope limitation (documented, not fixed this phase -- see
# 17-CONTEXT.md Deferred Ideas: "Full trusted-proxy chain parsing"):
# TRUST_PROXY trusts *any* value in the named header from *any* peer that
# reaches the app process -- it does not verify the connecting peer is
# actually the operator's proxy (no proxy-IP allowlist, no hop-count
# validation). This is safe ONLY if the app process is not directly
# reachable except through that proxy (e.g. bound to loopback with the proxy
# in front, or firewalled). Do not set this on a directly-internet-exposed
# bind and assume it is fully hardened.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").strip().lower()


def _trusted_forwarded_ip(request: Request):
    """None when TRUST_PROXY is unset, or the named header is absent. When
    present, returns the LEFTMOST value (the original client, per the
    de-facto X-Forwarded-For convention) -- multi-hop chain validation /
    trusted-proxy-IP allowlisting is explicitly deferred."""
    if not TRUST_PROXY:
        return None
    raw = request.headers.get(TRUST_PROXY)
    if not raw:
        return None
    return raw.split(",")[0].strip() or None


def is_trusted_loopback(request: Request) -> bool:
    """True ONLY for a DIRECT loopback connection with no proxy/tunnel
    forwarding headers (or, with TRUST_PROXY configured, for a DERIVED
    client that is itself loopback). Prisma's own in-process agent loopback
    calls carry none of the forwarding headers, so they still qualify."""
    if TRUST_PROXY:
        # Judge loopback-ness on the DERIVED client, never the proxy's own
        # (loopback) peer -- D-02. A TRUST_PROXY'd deployment must NOT
        # auto-grant LOCALHOST_BYPASS/admin-setup to every visitor just
        # because the fronting proxy connects from 127.0.0.1.
        derived = _trusted_forwarded_ip(request)
        return derived is not None and derived in ("127.0.0.1", "::1")
    client = getattr(request, "client", None)
    host = client.host if client else None
    if host not in ("127.0.0.1", "::1"):
        return False
    return not _has_forwarding_header(request)


def client_identity(request: Request) -> str:
    """Trusted identity for rate-limit keying (SEC-18) and any other caller
    needing 'the real client'. Shares the same TRUST_PROXY derivation core as
    is_trusted_loopback so the two can never disagree (D-02/D-04). Falls back
    to request.client.host (status quo) when TRUST_PROXY is unset or the
    header is absent -- an untrusted proxy's traffic collapses to one bucket,
    which is the correct fail-safe."""
    derived = _trusted_forwarded_ip(request)
    if derived:
        return derived
    client = getattr(request, "client", None)
    return (client.host if client else None) or "unknown"


def require_admin(request: Request):
    """Raise 403 if the current user isn't an admin.
    Allows access when auth is explicitly disabled, or when the request carries
    the in-process internal-tool token used by loopback agent tools.
    """
    # In-process bypass for tool-layer loopback calls. Two paths:
    # (a) header-direct (caller set X-Prisma-Internal-Token) — like
    #     AuthMiddleware, the token only counts from a trusted loopback
    #     client, so a leaked/forwarded header can't grant admin remotely, or
    # (b) AuthMiddleware already validated the token+loopback and set the
    #     middleware-only request.state.is_internal_tool flag (SEC-26/D-05).
    try:
        hdr = request.headers.get(INTERNAL_TOOL_HEADER)
        if hdr and secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN) and is_trusted_loopback(request):
            return
        # SEC-26 (D-05/D-06): grant on the middleware-only FLAG, never on the
        # current_user string -- current_user could theoretically be set to
        # "internal-tool" by some future code path (or a bug) without going
        # through the validated token+loopback branch that sets the flag.
        # Re-asserting is_trusted_loopback here is belt-and-suspenders even
        # though AuthMiddleware already checked it before setting the flag.
        # The current_user="internal-tool" string remains for logging /
        # attribution only; RESERVED_USERNAMES (core/auth.py) keeps it from
        # ever colliding with a real account (defense-in-depth).
        if getattr(request.state, "is_internal_tool", False) and is_trusted_loopback(request):
            return
    except Exception:
        pass

    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return
    if not auth_mgr or not auth_mgr.is_configured:
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate a per-request nonce for inline scripts
        nonce = secrets.token_hex(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)
        path = request.url.path

        # Tool render endpoints are served inside iframes — allow framing by self
        is_tool_render = path.startswith("/api/tools/") and path.endswith("/render")
        # Visual report pages are self-contained HTML — need inline scripts + external images
        is_report = path.startswith("/api/research/report/")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"

        if is_report:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        elif is_tool_render:
            # Tool iframe content: skip all framing headers — the iframe's
            # sandbox="allow-scripts" attribute provides isolation.
            # Don't overwrite the route's own restrictive CSP either.
            pass
        else:
            response.headers["X-Frame-Options"] = "DENY"
            # NOTE: `style-src 'unsafe-inline'` is intentionally retained.
            # `static/index.html` and `static/login.html` ship inline <style>
            # blocks, and several JS modules build runtime `style=""` attrs.
            # Migrating to nonce-only requires templating the HTML files +
            # auditing every JS-set style attribute. Since inline styles
            # don't execute script, the residual risk is visual-only.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob:; "
                "media-src 'self' blob:; "
                "connect-src 'self'; "
                "frame-src 'self'; "
                "frame-ancestors 'none'"
            )
        return response
