"""Shared pairing helpers for the companion bridge.

Token minting + LAN discovery + QR rendering, kept here as small, importable
units so the route layer stays thin and the logic is directly testable.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import uuid

import bcrypt

PAIRING_VERSION = 2  # SEC-22 (D-01): payload now carries a redeemable `code`,
# never a usable bearer token -- bumped from 1 because the wire semantics
# genuinely changed (see pairing_payload below).
COMPANION_SCOPE = "chat"

# SEC-22 (D-02): short-lived, single-use pairing-code pending map, modeled on
# the vetted Copilot device-code pending map (routes/copilot_routes.py:39-56,
# _prune_expired at :147). Lives in-process memory only -- restart loses any
# pending (unredeemed) code, which is an accepted tradeoff for a ~10-minute
# secret (same tradeoff the copilot precedent already made). Keyed by an
# 8-char prefix of the raw code itself (mirrors ApiToken.token_prefix's O(1)
# lookup idiom, core/database.py) so redemption never does a linear scan.
PAIRING_CODE_TTL_SECONDS = 600  # ~10 minutes (Claude discretion, D-02 default)

_PENDING_CODES: dict = {}
_PENDING_CODES_LOCK = threading.Lock()

# SEC-23 (D-04): double-submit CSRF cookie for the pairing mint (POST /pair)
# ONLY -- never /pair/redeem, whose caller has no ambient session cookie for
# a cross-site forger to ride (RESEARCH Pitfall 3).
CSRF_COOKIE_NAME = "prisma_pairing_csrf"


def new_csrf_token() -> str:
    """A fresh random CSRF token for the /pair double-submit cookie."""
    return secrets.token_urlsafe(32)


def csrf_tokens_match(cookie_value: str | None, submitted_value: str | None) -> bool:
    """Constant-time double-submit compare for POST /pair (SEC-23 D-04).

    Both values must be truthy BEFORE calling secrets.compare_digest --
    compare_digest("", "") returns True, which would let a request with
    neither a cookie nor a submitted value (exactly what a cross-site forger,
    who cannot set the victim's cookie, would send) pass the check
    (RESEARCH Pitfall 4).
    """
    if not cookie_value or not submitted_value:
        return False
    return secrets.compare_digest(cookie_value, submitted_value)


def default_port() -> int:
    """Best guess at the port the server is reachable on. Callers that know the
    real request port should pass it explicitly."""
    try:
        return int(os.environ.get("APP_PORT", "7000"))
    except ValueError:
        return 7000


def lan_ip_candidates() -> list[str]:
    """Likely LAN IPv4 addresses for this host, best candidate first.

    The UDP-connect trick reveals the egress interface the OS would use to reach
    the default gateway -- i.e. the address a phone on the same Wi-Fi should
    target. No packets are actually sent. Loopback is dropped.
    """
    candidates: list[str] = []

    def _add(ip):
        if ip and ip not in candidates and not ip.startswith("127."):
            candidates.append(ip)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        _add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass

    return candidates


def mint_token(owner: str, name: str = "companion") -> tuple[str, str]:
    """Create a chat-scoped API token row and return (token_id, raw_token).

    The raw token is returned ONCE -- only its bcrypt hash + an 8-char prefix
    are persisted. Mirrors routes/api_token_routes.py so cookie- and
    companion-minted tokens are indistinguishable to the auth middleware.

    Admin resolution for the live mint path is NOT done here -- the caller
    (the pairing route, behind require_admin) already attributes the mint to
    get_current_user(request); there is no first-user-in-a-dict fallback and
    there must never be one. Route any future admin lookup through
    AuthManager.is_admin, not a direct data-file parse.
    """
    from core.database import get_db_session, ApiToken

    raw_token = "ody_" + secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()
    token_id = str(uuid.uuid4())[:8]

    with get_db_session() as db:
        db.add(ApiToken(
            id=token_id,
            owner=owner,
            name=name,
            token_hash=token_hash,
            token_prefix=raw_token[:8],
            scopes=COMPANION_SCOPE,
            is_active=True,
        ))
    return token_id, raw_token


def _prune_expired_codes() -> None:
    """Drop any pending pairing-code entries past their TTL. Mirrors
    routes/copilot_routes.py's _prune_expired sweep (called lazily on access,
    no background timer)."""
    now = time.time()
    with _PENDING_CODES_LOCK:
        for k in [k for k, v in _PENDING_CODES.items() if v.get("expires_at", 0) < now]:
            _PENDING_CODES.pop(k, None)


def mint_pairing_code(owner: str) -> str:
    """Mint a short-lived, single-use pairing code. Returns the raw code
    (shown once, embedded in the QR/JSON payload). Only its bcrypt hash is
    retained server-side -- mirrors mint_token's hash-not-plaintext posture
    (SEC-22 D-02); the plaintext code is never persisted at rest.
    """
    _prune_expired_codes()
    raw_code = "pcd_" + secrets.token_urlsafe(24)
    code_hash = bcrypt.hashpw(raw_code.encode(), bcrypt.gensalt()).decode()
    with _PENDING_CODES_LOCK:
        _PENDING_CODES[raw_code[:8]] = {
            "code_hash": code_hash,
            "owner": owner,
            "expires_at": time.time() + PAIRING_CODE_TTL_SECONDS,
        }
    return raw_code


def redeem_pairing_code(raw_code: str) -> str | None:
    """Redeem a pairing code exactly once. Returns the owner on success, None
    on ANY failure (unknown/expired/already-redeemed/wrong code) -- fail
    closed with no distinguishing detail, so a caller can't tell "expired"
    from "wrong" from "already used" (avoids an enumeration oracle, SEC-22).

    Single-use is enforced by popping the matched entry BEFORE returning,
    inside the SAME lock as the lookup+checkpw -- a concurrent double-redeem
    of the same code cannot both pass.
    """
    _prune_expired_codes()
    prefix = (raw_code or "")[:8]
    with _PENDING_CODES_LOCK:
        entry = _PENDING_CODES.get(prefix)
        if not entry or not raw_code or not bcrypt.checkpw(raw_code.encode(), entry["code_hash"].encode()):
            return None
        # Pop BEFORE returning, still inside the lock (single-use, SEC-22 core).
        _PENDING_CODES.pop(prefix, None)
        owner = entry["owner"]
    return owner


def mint_companion_token(owner: str, name: str = "companion") -> str:
    """Mint the durable companion ApiToken issued at redemption time (SEC-22).

    Like mint_token, but with a bounded, non-NULL expires_at (~90 days,
    D-03 Claude-discretion default) -- this is what makes a redeemed pairing
    code resolve to a NON-permanent token, unlike the old direct-mint flow.
    mint_token itself is left untouched: routes/api_token_routes.py still
    needs the un-expiring (NULL expires_at) shape for its own token creation.

    Returns the raw token ONCE; only its bcrypt hash + prefix are persisted.
    """
    from datetime import timedelta

    from core.database import ApiToken, get_db_session, utcnow_naive

    raw_token = "ody_" + secrets.token_urlsafe(32)
    token_hash = bcrypt.hashpw(raw_token.encode(), bcrypt.gensalt()).decode()

    with get_db_session() as db:
        db.add(ApiToken(
            id=str(uuid.uuid4())[:8],
            owner=owner,
            name=name,
            token_hash=token_hash,
            token_prefix=raw_token[:8],
            scopes=COMPANION_SCOPE,
            is_active=True,
            expires_at=utcnow_naive() + timedelta(days=90),
        ))
    return raw_token


def pairing_payload(host: str, port: int, code: str) -> dict:
    """The exact JSON a client scans / accepts. Keep keys stable.

    Carries a redeemable single-use pairing CODE (SEC-22 D-01) -- never a
    usable bearer token. The client must POST this code to
    /api/companion/pair/redeem to exchange it for the durable ody_ token.
    """
    return {"v": PAIRING_VERSION, "host": host, "port": port, "code": code}


def pairing_qr_png_data_uri(payload: dict) -> str | None:
    """Render the pairing payload as a QR `data:` URI for an <img>. Returns None
    if the optional qrcode dep is unavailable."""
    try:
        import base64
        import io

        import qrcode

        img = qrcode.make(json.dumps(payload, separators=(",", ":")))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
