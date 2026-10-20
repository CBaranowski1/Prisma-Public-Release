# app.py — slim orchestrator
import mimetypes
import os


def register_static_mime_types() -> None:
    """Force stable JS module MIME types across platforms.

    Some native Windows setups inherit stale/incorrect registry mappings for
    ``.js``/``.mjs``, which can make Starlette serve ES modules with a non-JS
    ``Content-Type`` and cause the UI to load but fail on click. Re-register the
    standard MIME types at startup so static assets are served consistently.
    """

    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")


register_static_mime_types()

from dotenv import load_dotenv
# encoding="utf-8-sig" tolerates a UTF-8 BOM in .env — a common Windows gotcha
# when the file is saved from Notepad. Without this, the first key parses as
# "﻿AUTH_ENABLED" instead of "AUTH_ENABLED", so AUTH_ENABLED=false (etc.)
# is silently ignored and the user is unexpectedly forced to log in (issue #142).
# utf-8-sig reads plain UTF-8 (no BOM) identically, so this is safe everywhere.
load_dotenv(encoding="utf-8-sig")

# Prisma is a private, single-user desktop application. Authentication and its
# login screen are intentionally unavailable. Set this before importing route
# modules so every dependency sees the same permanent single-user mode even if
# an old deployment still has AUTH_ENABLED=true in its .env file.
os.environ["AUTH_ENABLED"] = "false"
os.environ["LOCALHOST_BYPASS"] = "false"

# Force HuggingFace/fastembed to COPY model files instead of symlinking on a
# network-backed data dir (cross-OS; see core/hf_env_guard.py). Runs AFTER
# load_dotenv() so a .env-only PRISM_DATA_DIR override is visible to the
# network-dir check. huggingface_hub reads this at import time, so it must be
# set before anything pulls it in. (Mirrored in src/embeddings.py for
# non-server entrypoints.)
from core.hf_env_guard import ensure_hf_symlinks_disabled_if_networked
ensure_hf_symlinks_disabled_if_networked()

import asyncio
import logging
import secrets
from datetime import datetime
from typing import Dict, Optional

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Core imports
from core.constants import (
    BASE_DIR, STATIC_DIR, SESSIONS_FILE,
    REQUEST_TIMEOUT, OPENAI_API_KEY, DATA_DIR,
)
from core.database import SessionLocal, ApiToken
from core.middleware import SecurityHeadersMiddleware
from core.auth import get_auth_manager
from core.exceptions import (
    SessionNotFoundError, InvalidFileUploadError,
    LLMServiceError, WebSearchError,
)

import bcrypt as _bcrypt

from src.app_helpers import abs_join
from starlette.responses import RedirectResponse

# ========= LOGGING =========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


def configure_file_logging(data_dir: str) -> str:
    """Attach a rotating file handler at <data_dir>/logs/server.log to the root
    logger so a console-less (packaged/server) run is still debuggable.
    Idempotent: a second call is a no-op."""
    import os
    from logging.handlers import RotatingFileHandler

    log_dir = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "server.log")
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and getattr(h, "_prism_file", None) == log_path:
            return log_path
    handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler._prism_file = log_path
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root.addHandler(handler)
    return log_path


from core.constants import DATA_DIR as _DATA_DIR
try:
    configure_file_logging(_DATA_DIR)
except Exception:
    logging.getLogger(__name__).warning("file logging unavailable", exc_info=True)

# ========= APP =========
# Lifespan is defined below (after all helpers it references are in scope)
# and passed to FastAPI so we can use the modern context-manager lifecycle
# instead of the deprecated @app.on_event("startup"/"shutdown") decorators.
app = FastAPI(
    title="AI Chat Application",
    description="Comprehensive AI chat with memory, research, and multi-modal capabilities",
    version="1.0.0",
)

# ========= CORS =========
allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=[
        "Accept",
        "Authorization",
        "Content-Type",
        "X-API-Key",
        "X-Auth-Token",
        "X-Prisma-Internal-Token",
        "X-Prisma-Owner",
        "X-Requested-With",
        "X-TZ-Offset",
    ],
)

# ========= SECURITY HEADERS MIDDLEWARE =========
app.add_middleware(SecurityHeadersMiddleware)


# ========= REQUEST TIMEOUT (FALLBACK FOR HUNG HANDLERS) =========
# If a single request takes longer than REQUEST_HARD_TIMEOUT, abort it and
# return 504 instead of holding the event loop hostage. Whitelisted paths
# (streaming, long-running shell exec, research) are exempt because they
# legitimately stay open. Without this, a single hung subprocess.run or
# missing-timeout httpx call locks up the entire server for everyone.
import asyncio as _asyncio
from starlette.middleware.base import BaseHTTPMiddleware as _BaseHTTPMiddleware
from starlette.responses import JSONResponse as _JSONResponse

REQUEST_HARD_TIMEOUT = float(os.getenv("REQUEST_HARD_TIMEOUT", "45"))
_TIMEOUT_EXEMPT_PREFIXES = (
    "/api/chat",            # streaming
    "/api/model-ide/complete",  # local code reviews may include model reasoning
    "/api/shell/stream",    # SSE
    "/api/research",        # multi-minute jobs
    "/api/model/download",  # tmux setup may run pip installs
    "/api/model/probe",     # SSE; iterates models with up to 8s timeout each
    "/api/model-endpoints", # /probe sub-route also iterates models
    "/api/cookbook/setup",  # remote pacman/apt installs
    "/api/upload",          # large files
    "/api/image",           # diffusion proxies (inpaint/harmonize/upscale/etc.) — own 120s httpx timeout
)


class _RequestTimeoutMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path or ""
        if any(path.startswith(p) for p in _TIMEOUT_EXEMPT_PREFIXES):
            return await call_next(request)
        try:
            return await _asyncio.wait_for(call_next(request), timeout=REQUEST_HARD_TIMEOUT)
        except _asyncio.TimeoutError:
            return _JSONResponse(
                {"detail": f"Request exceeded {REQUEST_HARD_TIMEOUT:.0f}s timeout"},
                status_code=504,
            )


app.add_middleware(_RequestTimeoutMiddleware)

# ========= AUTH =========
from routes.auth_routes import setup_auth_routes, SESSION_COOKIE

auth_manager = get_auth_manager()  # SEC-27c/D-07c: shared process-level singleton

# SEC-21 D-10: the passwordless dev-session mint (PRISMA_DEV_SWITCHER) must
# never enable on a network-exposed run. This process cannot introspect its
# own bind address at import time (uvicorn hasn't bound the socket yet, and
# reading PRISM_HOST directly here would be a false-safe bug -- it's never
# set in the Dockerfile's `CMD --host 0.0.0.0` or start-macos.sh's bare
# `uvicorn` invocation, both of which bypass server_main.py entirely). Only
# server_main.py's argparse-resolved --host value is trustworthy; it writes
# that value into `_PRISM_RESOLVED_BIND_HOST` BEFORE importing this module.
# Absence of the signal is "unknown" -> assume exposed -> fail closed.
_DEV_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _assert_dev_mint_allowed(resolved_host: Optional[str]) -> None:
    """Raise RuntimeError unless `resolved_host` confirms a loopback-only
    bind. Extracted as a standalone function so it can be exercised directly
    by tests without a full app re-import per case (SEC-21 D-10)."""
    if resolved_host not in _DEV_LOOPBACK_HOSTS:
        raise RuntimeError(
            "PRISMA_DEV_SWITCHER is set but the server's bind host could not be "
            "confirmed as loopback-only (resolved_host=%r). Refusing to seed dev "
            "accounts. Launch via `python server_main.py --host 127.0.0.1` to use "
            "the dev switcher." % resolved_host
        )


if os.getenv("PRISMA_DEV_SWITCHER", "").lower() in ("1", "true", "yes"):
    _assert_dev_mint_allowed(os.environ.get("_PRISM_RESOLVED_BIND_HOST"))
    try:
        _seeded = auth_manager.ensure_dev_accounts(os.getenv("PRISMA_DEV_PASSWORD", "password"))
        if _seeded:
            logging.getLogger(__name__).info("Seeded %d dev account(s): admin/coder/user", _seeded)
    except Exception:
        logging.getLogger(__name__).warning("Dev account seeding failed", exc_info=True)
app.state.auth_manager = auth_manager
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() != "false"
LOCALHOST_BYPASS = os.getenv("LOCALHOST_BYPASS", "false").lower() == "true"
if LOCALHOST_BYPASS:
    logger.warning("LOCALHOST_BYPASS is enabled, loopback requests bypass authentication. Do not expose this instance to a network.")

if AUTH_ENABLED:
    AUTH_EXEMPT_EXACT = {
        "/api/auth/setup",
        "/api/auth/signup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/status",
        "/api/auth/features",
        "/api/auth/settings",
        "/api/auth/integrations/presets",
        "/api/health",
        "/api/ready",   # Docker HEALTHCHECK curls this unauthenticated (CR-01)
        "/api/version",
        "/login",
        # SEC-22 (D-01): the pairing code IS the credential for this one
        # call -- a device redeeming it has no cookie and no bearer token
        # yet (that's the whole point of the handshake). Mirrors the
        # webhook-token exemption below; the handler itself bcrypt-checks
        # the code via redeem_pairing_code and fails closed (401). Without
        # this exemption AuthMiddleware would reject the POST with 401
        # before the handler's own check ever runs.
        "/api/companion/pair/redeem",
    }
    AUTH_EXEMPT_PREFIXES = ["/static", "/v1"]  # /v1 facade self-guards via PRISM_TOKEN (compare_digest)

    # SEC-20 D-09: a SEPARATE, narrower allowlist from AUTH_EXEMPT_* above.
    # AUTH_EXEMPT_* controls which paths need NO authentication at all;
    # this one controls which AUTHENTICATED-but-flagged paths a
    # must_change_password user may still reach. Keep this allowlist
    # minimal (change-password/logout/status only, Pitfall 8) -- the
    # flagged state is a narrow, short-lived corridor to one exit (setting
    # a new password), not a general-purpose "half-authenticated" mode. A
    # future authenticated route a flagged user legitimately needs does
    # NOT automatically belong here -- think twice before adding to it.
    _MUST_CHANGE_PW_ALLOWLIST = {
        "/api/auth/change-password",
        "/api/auth/logout",
        "/api/auth/status",
    }
    # Dynamic paths whose own handler proves identity via a path-embedded
    # secret instead of the session/bearer auth. The route handler at
    # routes/task_routes.py validates the per-task `webhook_token` itself
    # and returns 404 on mismatch, so the path is the credential — the
    # UI labels these URLs "no auth needed" precisely because external
    # callers (Zapier, n8n, curl) can't supply a session cookie. Without
    # this exemption AuthMiddleware rejects every POST with 401 before
    # the token is ever checked.
    import re as _re
    AUTH_EXEMPT_PATTERNS = [
        _re.compile(r"^/api/tasks/[^/]+/webhook/[^/]+/?$"),
    ]

    def _is_auth_exempt(path: str) -> bool:
        if path in AUTH_EXEMPT_EXACT:
            return True
        if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
            return True
        return any(p.match(path) for p in AUTH_EXEMPT_PATTERNS)

    # In-memory token cache: prefix → list[(token_id, token_hash, owner, scopes)]. The DB
    # query was running on every API-bearer request and scanning bcrypt
    # checks linearly. With this cache, we hit the DB only when the cache
    # version bumps (token created/revoked) — see _token_cache_invalidate
    # in app.state, called by routes/api_token_routes.
    _token_cache: dict = {}
    _token_cache_lock = _asyncio.Lock()
    _token_cache_dirty = True

    def _token_cache_invalidate():
        nonlocal_dict = app.state.__dict__
        nonlocal_dict["_token_cache_dirty"] = True
    app.state.invalidate_token_cache = _token_cache_invalidate
    app.state._token_cache = _token_cache
    app.state._token_cache_dirty = True

    def _refresh_token_cache():
        """Rebuild the prefix→[(id,hash,owner,scopes,expires_at)] map from the DB.

        SEC-22 D-03: filters out already-expired tokens server-side at
        refresh time (coarse defense — an expired token drops out of the
        cache the NEXT time it's rebuilt) in addition to the per-request
        expiry re-check in the match loop below (Pitfall 5 — both sites
        must move to a 5-tuple in lockstep).
        """
        from collections import defaultdict
        from core.database import utcnow_naive
        new_map = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.query(ApiToken).filter(
                ApiToken.is_active == True,
                (ApiToken.expires_at.is_(None)) | (ApiToken.expires_at > utcnow_naive()),
            ).all()
            for r in rows:
                scopes = [s.strip() for s in (getattr(r, "scopes", "") or "chat").split(",") if s.strip()]
                new_map[r.token_prefix].append((r.id, r.token_hash, getattr(r, "owner", None), scopes, r.expires_at))
        finally:
            db.close()
        _token_cache.clear()
        _token_cache.update(new_map)
        app.state._token_cache_dirty = False

    # True ONLY for a DIRECT loopback connection with no proxy/tunnel
    # forwarding headers (cloudflared / nginx / Caddy connect FROM 127.0.0.1,
    # so a bare client.host check would let remote visitors inherit local
    # trust). Shared with require_admin's internal-token path and the
    # first-run /api/auth/setup gate — see core/middleware.py.
    from core.middleware import is_trusted_loopback as _is_trusted_loopback

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            if _is_auth_exempt(path):
                return await call_next(request)
            # In-process internal-tool token bypass. Used by the agent
            # tool layer when it HTTP-loopbacks to admin-gated routes
            # (no admin cookie available in that context). Restricted to
            # loopback clients + matching token to keep it locked down.
            try:
                from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN as _ITT
                _hdr = request.headers.get(INTERNAL_TOOL_HEADER)
                if _hdr and secrets.compare_digest(_hdr, _ITT) and _is_trusted_loopback(request):
                    # Impersonation: when the agent's loopback call sets
                    # X-Prisma-Owner, attribute the request to that user only
                    # if they exist. Authorization checks remain separate; this
                    # is just owner attribution for notes/calendar/etc.
                    _impersonate = (request.headers.get("X-Prisma-Owner") or "").strip()
                    _auth_mgr = getattr(request.app.state, "auth_manager", None) or auth_manager
                    if _impersonate and _impersonate in getattr(_auth_mgr, "users", {}):
                        request.state.current_user = _impersonate
                    else:
                        request.state.current_user = "internal-tool"
                    # SEC-26 (D-05): the ONLY place this flag is ever set --
                    # require_admin grants the internal-tool path on THIS
                    # middleware-only boolean, never on the current_user
                    # string above (a future path/bug could otherwise set
                    # that string without going through this validated
                    # compare_digest+loopback branch).
                    request.state.is_internal_tool = True
                    request.state.api_token = False
                    return await call_next(request)
            except Exception:
                pass
            # Allow DIRECT localhost requests (internal service calls from
            # heartbeats etc.). Tunnel/proxy-forwarded requests are excluded by
            # _is_trusted_loopback so LOCALHOST_BYPASS can't be abused over a
            # Cloudflare tunnel / reverse proxy. Keep LOCALHOST_BYPASS=false for
            # network-exposed deployments regardless.
            if LOCALHOST_BYPASS and _is_trusted_loopback(request):
                return await call_next(request)
            if not auth_manager.is_configured:
                # No users yet — redirect to login for first-time setup
                if not path.startswith("/api/"):
                    return RedirectResponse(url="/login", status_code=302)
                return JSONResponse(status_code=401, content={"error": "Setup required"})

            # --- Bearer token auth (API tokens for external integrations) ---
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer ody_"):
                raw_token = auth_header[7:]
                # Sanity check: tokens are "ody_" + 43 chars of base64
                if len(raw_token) < 12 or len(raw_token) > 100:
                    return JSONResponse(status_code=401, content={"error": "Invalid API token"})
                prefix = raw_token[:8]
                try:
                    if app.state._token_cache_dirty:
                        async with _token_cache_lock:
                            if app.state._token_cache_dirty:
                                await _asyncio.to_thread(_refresh_token_cache)
                    candidates = list(_token_cache.get(prefix, ()))
                    matched_id = None
                    matched_owner = None
                    matched_scopes = []
                    # SEC-22: expiry is enforced here AND in _refresh_token_cache's
                    # SQL filter above (both sites, in lockstep -- RESEARCH
                    # Pitfall 5). The assembled-app proof that a genuinely
                    # expired bearer token gets a real 401 through this exact
                    # AuthMiddleware path is delivered by Plan 06 (SC-4); this
                    # plan (16-01) proves the selection logic at the unit level
                    # (tests/test_token_expiry.py) and this two-site wiring.
                    from core.database import api_token_is_live as _api_token_is_live
                    for tid, thash, owner, scopes, expires_at in candidates:
                        # Defense-in-depth (SEC-22 D-03, RESEARCH Pitfall 5):
                        # the cache is only invalidated on mint/revoke, not
                        # on a timer, so a token could expire mid-cache-life.
                        # Re-check expiry here even though _refresh_token_cache
                        # already filters at the SQL level.
                        if not _api_token_is_live(expires_at):
                            continue
                        if _bcrypt.checkpw(raw_token.encode(), thash.encode()):
                            matched_id = tid
                            matched_owner = owner
                            matched_scopes = scopes or []
                            break
                    if matched_id:
                        # Update last_used_at off the hot path. Doing it
                        # inline used to keep the request open across an
                        # extra commit; do it fire-and-forget instead.
                        async def _touch_last_used(tid: str):
                            def _do():
                                _db = SessionLocal()
                                try:
                                    _db.query(ApiToken).filter(ApiToken.id == tid).update(
                                        {"last_used_at": datetime.utcnow()}
                                    )
                                    _db.commit()
                                finally:
                                    _db.close()
                            try:
                                await _asyncio.to_thread(_do)
                            except Exception:
                                pass
                        _asyncio.create_task(_touch_last_used(matched_id))
                        # Keep bearer-token callers out of normal cookie/user
                        # routes. API-aware routes can read api_token_owner.
                        request.state.current_user = "api"
                        request.state.api_token = True
                        request.state.api_token_id = matched_id
                        request.state.api_token_owner = matched_owner
                        request.state.api_token_scopes = matched_scopes
                        return await call_next(request)
                except Exception:
                    logger.warning("API token auth error", exc_info=False)
                # Invalid bearer token — reject immediately
                return JSONResponse(status_code=401, content={"error": "Invalid API token"})

            # --- Cookie-based session auth ---
            token = request.cookies.get(SESSION_COOKIE)
            if not auth_manager.validate_token(token):
                if path.startswith("/api/"):
                    return JSONResponse(status_code=401, content={"error": "Not authenticated"})
                return RedirectResponse(url="/login", status_code=302)

            # Attach current username to request state for downstream routes
            request.state.current_user = auth_manager.get_username_for_token(token)
            request.state.api_token = False
            # SEC-20 D-09: a flagged user is authenticated but must be
            # confined to the change-password corridor until they rotate
            # their (admin-reset or CLI-break-glass) temporary password.
            # See _MUST_CHANGE_PW_ALLOWLIST above (Pitfall 8: a distinct,
            # narrower allowlist from AUTH_EXEMPT_* -- do not conflate them).
            if getattr(auth_manager, "users", {}).get(request.state.current_user, {}).get("must_change_password"):
                if path not in _MUST_CHANGE_PW_ALLOWLIST:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": "must_change_password",
                            "detail": "Change your password before continuing.",
                        },
                    )
            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    logger.info("Auth middleware enabled (AUTH_ENABLED=true)")
else:
    logger.info("Auth middleware disabled (set AUTH_ENABLED=true to enable)")

# ========= STATIC FILES =========
os.makedirs(STATIC_DIR, exist_ok=True)


class _RevalidatingStatic(StaticFiles):
    """Serve static assets normally, but force the browser to REVALIDATE
    source files (.js/.css/.html) on every load instead of serving a stale
    copy from disk cache. The app ships raw ES modules with no build step or
    versioned URLs, so browsers were caching modules across deploys — a code
    change wouldn't appear without a manual hard-refresh. `no-cache` keeps the
    cached bytes but requires a conditional request; unchanged files still
    return a cheap 304 (ETag/Last-Modified are preserved)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")

# ========= GENERATED IMAGES =========
@app.get("/api/generated-image/{filename}")
async def serve_generated_image(filename: str, request: Request):
    """Serve generated images from the data directory."""
    from pathlib import Path
    import re
    if not re.match(r'^[a-f0-9]{8,64}\.(png|jpg|jpeg|webp|gif|mp4|mov|webm|mkv|m4v)$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    img_path = Path(DATA_DIR) / "generated_images" / filename
    if not img_path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    # SECURITY: filename is the only key, so anyone who knows / guesses a
    # 12-hex content hash could pull another user's image bytes. Require
    # auth and verify ownership via the gallery row (when one exists).
    try:
        from src.auth_helpers import get_current_user
        from core.database import SessionLocal as _SL, GalleryImage as _GI
        _user = get_current_user(request)
        if _user:
            _db = _SL()
            try:
                _row = _db.query(_GI).filter(_GI.filename == filename).first()
                # Generated-but-not-yet-imported images have no row → allow.
                # Row exists with a different owner → 404 (don't confirm existence).
                if _row is not None and _row.owner and _row.owner != _user:
                    raise HTTPException(status_code=404, detail="Image not found")
            finally:
                _db.close()
    except HTTPException:
        raise
    except Exception:
        pass
    ext = filename.rsplit('.', 1)[-1].lower()
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif",
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "mkv": "video/x-matroska", "m4v": "video/mp4",
    }.get(ext, "application/octet-stream")
    # Generated-image filenames are content hashes → the bytes for a given
    # filename never change. Cache them hard so the gallery doesn't
    # re-download every full-size image each time it's opened. `immutable`
    # tells the browser it never needs to revalidate within the max-age.
    return FileResponse(
        str(img_path),
        media_type=mime,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )

# ========= YOUTUBE INIT =========
from services.youtube import init_youtube
init_youtube()

# ========= RAG (vector document RAG) =========
# VectorRAG (ChromaDB-backed personal-document semantic search). Initialized
# lazily via get_rag_manager() — returns None if ChromaDB isn't reachable
# (no server running on the configured host:port), in which case personal-doc
# routes return a clean 503 instead of busy-retrying every request.
#
# Note: this was previously hardcoded off because chromadb 1.4.1 / pydantic
# 2.12 were mutually incompatible at the time. With the current pins
# (chromadb 1.5.x + pydantic 2.13.x) the init works and Personal Docs
# (POST /api/personal/add_directory etc.) is functional again.
from src.rag_singleton import get_rag_manager
rag_manager = get_rag_manager()
rag_available = rag_manager is not None
if rag_available:
    logger.info("Vector document RAG initialized")
else:
    logger.info(
        "Vector document RAG not available at startup "
        "(ChromaDB may not be reachable yet — routes will retry lazily)"
    )

# ========= IMPORT CONFIG =========
from src.config import config

# ========= COMPONENT INITIALIZATION =========
from src.app_initializer import initialize_managers

components = initialize_managers(BASE_DIR, rag_manager)

session_manager   = components["session_manager"]
from src.assistant_log import set_session_manager as _set_asst_sm
_set_asst_sm(session_manager)
memory_manager    = components["memory_manager"]
memory_vector     = components.get("memory_vector")
upload_handler    = components["upload_handler"]
personal_docs_mgr = components["personal_docs_manager"]
api_key_manager   = components["api_key_manager"]
preset_manager    = components["preset_manager"]
chat_processor    = components["chat_processor"]
research_handler  = components["research_handler"]
chat_handler      = components["chat_handler"]
model_discovery   = components["model_discovery"]
skills_manager    = components["skills_manager"]
memory_provider_registry = components["memory_provider_registry"]
app.state.memory_provider_registry = memory_provider_registry

# TTS
from services.tts import get_tts_service

tts_service = get_tts_service()
logger.info("TTS service initialized (provider managed via admin settings)")

# ========= EXCEPTION HANDLERS =========
@app.exception_handler(SessionNotFoundError)
async def session_not_found_handler(request: Request, exc: SessionNotFoundError):
    return JSONResponse(status_code=404, content={"error": "SESSION_NOT_FOUND", "message": str(exc)})

@app.exception_handler(InvalidFileUploadError)
async def invalid_file_upload_handler(request: Request, exc: InvalidFileUploadError):
    return JSONResponse(status_code=400, content={"error": "INVALID_FILE_UPLOAD", "message": str(exc)})

@app.exception_handler(LLMServiceError)
async def llm_service_error_handler(request: Request, exc: LLMServiceError):
    return JSONResponse(status_code=502, content={"error": "LLM_SERVICE_ERROR", "message": str(exc)})

@app.exception_handler(WebSearchError)
async def web_search_error_handler(request: Request, exc: WebSearchError):
    return JSONResponse(status_code=502, content={"error": "WEB_SEARCH_ERROR", "message": str(exc)})

# ========= WEBHOOK MANAGER =========
from src.webhook_manager import WebhookManager

webhook_manager = WebhookManager(api_key_manager=api_key_manager)

# ========= INCLUDE ROUTERS =========

# Auth
auth_router = setup_auth_routes(auth_manager)
app.include_router(auth_router)

# Uploads
from routes.upload_routes import setup_upload_routes
upload_router, upload_cleanup_func = setup_upload_routes(upload_handler)
app.include_router(upload_router)
upload_cleanup_task = None

# Emoji SVG proxy (same-origin, lazy-cached Twemoji) — lets the chat render
# emojis as flat SVG instead of system color glyphs.
from routes.emoji_routes import setup_emoji_routes
app.include_router(setup_emoji_routes())

# Sessions
from routes.session_routes import setup_session_routes
session_config = {"REQUEST_TIMEOUT": REQUEST_TIMEOUT, "OPENAI_API_KEY": OPENAI_API_KEY, "SESSIONS_FILE": SESSIONS_FILE}
app.include_router(setup_session_routes(session_manager, session_config, webhook_manager=webhook_manager))

# Admin Danger Zone wipes (Settings → System → Danger Zone)
from routes.admin_wipe_routes import setup_admin_wipe_routes
app.include_router(setup_admin_wipe_routes(session_manager))

# Memory
from routes.memory_routes import setup_memory_routes
memory_router = setup_memory_routes(memory_manager, session_manager, memory_vector=memory_vector)
app.include_router(memory_router)
from routes.skills_routes import setup_skills_routes
app.include_router(setup_skills_routes(skills_manager))
from routes.model_ide_routes import setup_model_ide_routes
app.include_router(setup_model_ide_routes())
from routes.openai_compat_routes import setup_openai_compat_routes
app.include_router(setup_openai_compat_routes())

# Chat
from routes.chat_routes import setup_chat_routes
app.include_router(setup_chat_routes(
    session_manager, chat_handler, chat_processor,
    memory_manager, research_handler, upload_handler,
    memory_vector=memory_vector,
    webhook_manager=webhook_manager,
    skills_manager=skills_manager,
))

# Research (background deep-research tasks)
from routes.research_routes import setup_research_routes
app.include_router(setup_research_routes(research_handler, session_manager=session_manager))

# History
from routes.history_routes import setup_history_routes
app.include_router(setup_history_routes(session_manager))

# Search
from routes.search_routes import setup_search_routes
app.include_router(setup_search_routes(config))

# Presets
from routes.preset_routes import setup_preset_routes
app.include_router(setup_preset_routes(preset_manager))

# Diagnostics
from routes.diagnostics_routes import setup_diagnostics_routes
app.include_router(setup_diagnostics_routes(rag_manager, rag_available, research_handler))

# Cleanup
from routes.cleanup_routes import setup_cleanup_routes
app.include_router(setup_cleanup_routes(session_manager))

# Personal docs
from routes.personal_routes import setup_personal_routes
app.include_router(setup_personal_routes(personal_docs_mgr, rag_manager, rag_available))

# Embedding model management
from routes.embedding_routes import setup_embedding_routes
app.include_router(setup_embedding_routes())

# Models
from routes.model_routes import setup_model_routes
app.include_router(setup_model_routes(model_discovery))

# GitHub Copilot device-flow login
from routes.copilot_routes import setup_copilot_routes
app.include_router(setup_copilot_routes())

# TTS
from routes.tts_routes import setup_tts_routes
app.include_router(setup_tts_routes(tts_service))

# STT
from services.stt import get_stt_service
stt_service = get_stt_service()
from routes.stt_routes import setup_stt_routes
app.include_router(setup_stt_routes(stt_service))
logger.info("STT service initialized (provider managed via settings)")

# Documents (artifacts/canvas)
from routes.document_routes import setup_document_routes
document_router = setup_document_routes(session_manager, upload_handler)
app.include_router(document_router)

# Signatures (reusable image stamps)
from routes.signature_routes import setup_signature_routes
app.include_router(setup_signature_routes())

# Gallery (image library)
from routes.gallery_routes import setup_gallery_routes
app.include_router(setup_gallery_routes())

# Persisted image-editor drafts (server-backed projects)
from routes.editor_draft_routes import setup_editor_draft_routes
app.include_router(setup_editor_draft_routes())

# Scheduled tasks + event bus
from src.task_scheduler import TaskScheduler
task_scheduler = TaskScheduler(session_manager)
from src.event_bus import set_task_scheduler
set_task_scheduler(task_scheduler)
from routes.task_routes import setup_task_routes
app.include_router(setup_task_routes(task_scheduler))

from routes.assistant_routes import setup_assistant_routes
app.include_router(setup_assistant_routes(task_scheduler))

# [removed-organizer] Email/Calendar/Notes/Contacts/Codex disabled for Prisma.
# Code retained in routes/ but not mounted. See docs/superpowers/specs/2026-06-10-prisma-design.md
email_router = None
calendar_router = None

# Calendar (CalDAV)  -- DISABLED
# from routes.calendar_routes import setup_calendar_routes
# calendar_router = setup_calendar_routes()
# app.include_router(calendar_router)

# Shell (user-facing command execution)
from routes.shell_routes import setup_shell_routes
app.include_router(setup_shell_routes())

# Cookbook (model download/serve/cache, cookbook state sync)
from routes.cookbook_routes import setup_cookbook_routes
app.include_router(setup_cookbook_routes())

# Hardware model fitting (cookbook "What Fits?" tab)
from routes.hwfit_routes import setup_hwfit_routes
app.include_router(setup_hwfit_routes())

# Model A/B Comparison
from routes.compare_routes import setup_compare_routes
app.include_router(setup_compare_routes(session_manager))

# Council — modular multi-model deliberation
from routes.council_routes import setup_council_routes
app.include_router(setup_council_routes(session_manager))

# Brain provider API (providers, search, categories)
from routes.brain_routes import setup_brain_routes
app.include_router(setup_brain_routes(memory_provider_registry))

# Brain graph visualization (node-graph over the vault)
from routes.brain_graph_routes import router as brain_graph_router
app.include_router(brain_graph_router)

# VERA agent registry (loads brains/<brain>/config/vera_agents/*.yaml)
from routes.vera_agents_routes import router as vera_agents_router
app.include_router(vera_agents_router)

# Gated answer pipeline (offline model -> brain retrieval -> VERA agent consensus)
from routes.answer_routes import router as answer_router
app.include_router(answer_router)

# Brain ingest (POST /api/brain/ingest — scraper JSON + UI multipart)
from routes.brain_ingest_routes import setup_brain_ingest_routes
app.include_router(setup_brain_ingest_routes(auth_manager=auth_manager))

# User Preferences
from routes.prefs_routes import setup_prefs_routes
app.include_router(setup_prefs_routes())

# Backup (export/import user data)
from routes.backup_routes import setup_backup_routes
app.include_router(setup_backup_routes(memory_manager, preset_manager, skills_manager))

from routes.font_routes import setup_font_routes
app.include_router(setup_font_routes())


# MCP (Model Context Protocol)
from src.mcp_manager import McpManager
from src.agent_tools import set_mcp_manager
from routes.mcp_routes import setup_mcp_routes

mcp_manager = McpManager()
set_mcp_manager(mcp_manager)
app.include_router(setup_mcp_routes(mcp_manager))
logger.info("MCP routes initialized")

# AI Interaction tools (debates, pipelines, self-managing AI, UI control)
from src.ai_interaction import set_session_manager as set_ai_session_manager, set_memory_manager as set_ai_memory_manager, set_rag_manager as set_ai_rag_manager
set_ai_session_manager(session_manager)
set_ai_memory_manager(memory_manager, memory_vector)
set_ai_rag_manager(rag_manager, personal_docs_mgr)
logger.info("AI interaction tools initialized (session, memory, RAG, UI control)")

# Webhooks
from routes.webhook_routes import setup_webhook_routes
app.include_router(setup_webhook_routes(webhook_manager, auth_manager, session_manager, api_key_manager))

# API Tokens
from routes.api_token_routes import setup_api_token_routes
app.include_router(setup_api_token_routes())

logger.info("Webhook & API token routes initialized")

# Notes (Google Keep-style notes/todos)  -- DISABLED [removed-organizer]
# from routes.note_routes import setup_note_routes
# app.include_router(setup_note_routes(task_scheduler))

# Email  -- DISABLED [removed-organizer]
# from routes.email_routes import setup_email_routes
# email_router = setup_email_routes()
# app.include_router(email_router)

# Codex integration — HTTP surface for the Codex plugin/MCP bridge. Reuses
# api_token scopes (todos:read|write, email:read|draft|send) so external
# Codex sessions can only touch the data the user explicitly allowed. Mounted
# AFTER email so the codex_routes can borrow the email router for shared
# search/threading helpers.  -- DISABLED [removed-organizer] (codex depends on email_router/calendar_router)
# from routes.codex_routes import setup_codex_routes, setup_claude_routes
# app.include_router(setup_codex_routes(
#     email_router=email_router,
#     memory_router=memory_router,
#     calendar_router=calendar_router,
#     document_router=document_router,
# ))
# app.include_router(setup_claude_routes())

from routes.vault_routes import setup_vault_routes
app.include_router(setup_vault_routes())

# Contacts (CardDAV)  -- DISABLED [removed-organizer]
# from routes.contacts_routes import setup_contacts_routes
# app.include_router(setup_contacts_routes())

from companion import setup_companion_routes
app.include_router(setup_companion_routes())

# ========= ROUTES (kept in app.py) =========

def _serve_html_with_nonce(request: Request, file_path: str) -> HTMLResponse:
    """Read an HTML file and inject the CSP nonce into inline <script> tags."""
    with open(file_path, "r", encoding="utf-8") as f:
        html = f.read()
    nonce = getattr(request.state, "csp_nonce", "")
    html = html.replace("{{CSP_NONCE}}", nonce)
    return HTMLResponse(html)

@app.get("/")
async def serve_index(request: Request):
    static_path = abs_join(BASE_DIR, "static/index.html")
    if os.path.exists(static_path):
        return _serve_html_with_nonce(request, static_path)
    root_path = abs_join(BASE_DIR, "index.html")
    if os.path.exists(root_path):
        return _serve_html_with_nonce(request, root_path)
    raise HTTPException(404, "index.html not found")

@app.get("/notes")
async def serve_notes(request: Request):
    return await serve_index(request)

@app.get("/calendar")
async def serve_calendar(request: Request):
    return await serve_index(request)

# Per-tool deep-link routes — all serve the same SPA, the JS auto-opens
# the matching modal based on window.location.pathname. Each route also
# gets a unique favicon + page title via inline script in index.html so
# bookmarks render with tool-specific icons.
@app.get("/cookbook")
async def serve_cookbook(request: Request):
    return await serve_index(request)

@app.get("/email")
async def serve_email(request: Request):
    return await serve_index(request)

@app.get("/memory")
async def serve_memory(request: Request):
    return await serve_index(request)

@app.get("/gallery")
async def serve_gallery(request: Request):
    return await serve_index(request)

@app.get("/tasks")
async def serve_tasks(request: Request):
    return await serve_index(request)

@app.get("/library")
async def serve_library(request: Request):
    return await serve_index(request)

@app.get("/backgrounds")
async def serve_backgrounds(request: Request):
    """Sandbox page for prototyping background effects. No auth required."""
    return _serve_html_with_nonce(request, abs_join(BASE_DIR, "static/backgrounds.html"))

@app.get("/api/version")
async def get_version():
    from core.constants import APP_VERSION
    return {"version": APP_VERSION}

@app.get("/api/health")
async def health_check() -> Dict[str, str]:
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

@app.get("/api/ready")
async def readiness_check() -> JSONResponse:
    """Readiness / integrity self-check — DB, data dir, local-first storage.

    Unlike /api/health (liveness), this returns 503 unless every critical
    subsystem is whole, so an orchestrator can gate traffic on real readiness.
    """
    from src.readiness import check_readiness
    result = check_readiness()
    return JSONResponse(status_code=200 if result.get("ready") else 503, content=result)

@app.get("/api/runtime")
async def runtime_info() -> Dict[str, object]:
    in_docker = os.path.exists("/.dockerenv")
    if not in_docker:
        try:
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as fh:
                cg = fh.read()
            in_docker = any(marker in cg for marker in ("docker", "containerd", "kubepods"))
        except Exception:
            in_docker = False
    ollama_url = (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_URL")
        or ("http://host.docker.internal:11434/v1" if in_docker else "http://127.0.0.1:11434/v1")
    )
    return {
        "in_docker": in_docker,
        "ollama_base_url": ollama_url,
    }

# ========= LIFECYCLE =========

@asynccontextmanager
async def _lifespan(app):
    """Modern lifespan context manager replacing deprecated @app.on_event."""
    # ── STARTUP ──
    await _startup_event()
    yield
    # ── SHUTDOWN ──
    await _shutdown_event()

app.router.lifespan_context = _lifespan


async def _startup_event():
    global upload_cleanup_task
    logger.info("Application starting up...")
    # Fail closed if a search compat shim has drifted from its canonical
    # services.search.* implementation — a broken deploy should abort boot
    # rather than silently serve stale ranking/search behavior (T-26-01).
    from src.search import audit_search_shim_identity
    audit_search_shim_identity()
    webhook_manager.set_loop(asyncio.get_running_loop())
    # Wipe any leftover incognito sessions from previous process — they're
    # ephemeral by design and must not survive a restart.
    try:
        from core.database import SessionLocal as _SL, Session as _DbSess, ChatMessage as _DbMsg
        _db = _SL()
        try:
            _ghosts = _db.query(_DbSess).filter(_DbSess.name.in_(("Nobody", "Incognito"))).all()
            for _g in _ghosts:
                _db.query(_DbMsg).filter(_DbMsg.session_id == _g.id).delete()
                _db.delete(_g)
            if _ghosts:
                _db.commit()
                logger.info(f"Purged {len(_ghosts)} leftover incognito session(s)")
        finally:
            _db.close()
    except Exception as e:
        logger.debug(f"Incognito purge skipped: {e}")
    # Strong refs to fire-and-forget startup tasks. Without this, Python may
    # GC tasks created with `asyncio.create_task(...)` before they finish.
    _startup_tasks: list[asyncio.Task] = getattr(app.state, "_startup_tasks", [])
    app.state._startup_tasks = _startup_tasks
    if upload_cleanup_func:
        upload_cleanup_task = asyncio.create_task(upload_cleanup_func())
    # Always-on monitor that auto-continues the agent when a background bash
    # job (#!bg) finishes — re-invokes the turn with the job output.
    try:
        from src.bg_monitor import start_bg_monitor
        _startup_tasks.append(start_bg_monitor())
    except Exception as _e:
        logger.warning("Failed to start background-job monitor: %s", _e)
    # MCP servers can be slow or blocked by local tooling. Connect them after
    # the web server is accepting traffic instead of delaying the whole UI.
    async def _startup_mcp_connections():
        try:
            from src.builtin_mcp import register_builtin_servers
            await register_builtin_servers(mcp_manager)
        except BaseException as e:
            logger.warning(f"Built-in MCP registration failed (non-critical): {type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(mcp_manager.connect_all_enabled(), timeout=20)
        except asyncio.TimeoutError:
            logger.warning("User MCP startup timed out (non-critical)")
        except BaseException as e:
            logger.warning(f"MCP startup failed (non-critical): {type(e).__name__}: {e}")

    _startup_tasks.append(asyncio.create_task(_startup_mcp_connections()))

    # Pre-warm the RAG tool index off the request path. Loading the local
    # embedding model + opening ChromaDB + indexing the built-in tools is a
    # one-time ~1-3s cost that otherwise lands on the user's FIRST message
    # (showing up as a big `tool_selection` time). Doing it here makes the
    # first turn as fast as subsequent ones (warm embed ≈ a few ms).
    async def _warmup_tool_index():
        try:
            from src.tool_index import get_tool_index
            idx = await asyncio.to_thread(get_tool_index)
            if idx:
                await asyncio.to_thread(idx.get_tools_for_query, "warmup", 8)
                logger.info("[startup] Tool index pre-warmed")
        except Exception as e:
            logger.warning(f"Tool index warmup failed (non-critical): {type(e).__name__}: {e}")

    _startup_tasks.append(asyncio.create_task(_warmup_tool_index()))
    # Warmup: ping all known LLM endpoints to prime connections
    async def _warmup_endpoints():
        try:
            import httpx
            endpoints = model_discovery.get_endpoints() if model_discovery else []
            for ep in endpoints[:5]:
                url = ep.get("url", "").replace("/chat/completions", "/models")
                if url:
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            await client.get(url)
                        logger.info(f"Warmup ping OK: {url}")
                    except Exception as e:
                        logger.debug(f"Warmup ping failed for endpoint: {e}")
        except Exception as e:
            logger.debug(f"Warmup ping skipped: {e}")

    _startup_tasks.append(asyncio.create_task(_warmup_endpoints()))

    # GRAY-INT-01: non-blocking startup probe that warms the provider
    # diagnostics snapshot cache; run_probe() catches all exceptions
    # internally and never blocks boot. Import + registration are guarded
    # like every sibling startup task above, so an import-time failure
    # (e.g. in src.provider_diagnostics or one of its imports) can't crash
    # application boot.
    try:
        from src import provider_diagnostics
        _startup_tasks.append(asyncio.create_task(provider_diagnostics.run_probe()))
    except Exception as e:
        logger.warning(f"Provider diagnostics probe failed to start (non-critical): {e}")

    # Keep-alive: ping endpoints every 60 seconds to prevent cold starts
    async def _keepalive_loop():
        while True:
            try:
                await asyncio.sleep(60)
                await _warmup_endpoints()
            except Exception as e:
                logger.warning(f"Keepalive loop error: {e}")
                await asyncio.sleep(300)  # Back off on error

    _startup_tasks.append(asyncio.create_task(_keepalive_loop()))

    async def _ensure_default_tasks():
        # Create/reconcile default automation tasks + personal assistant for every user.
        owners = set()
        try:
            import json as _json
            auth_path = os.path.join(DATA_DIR, "auth.json")
            with open(auth_path, encoding="utf-8") as f:
                users = _json.load(f).get("users", {})
            owners.update(users.keys())
        except Exception as e:
            logger.debug(f"Default task auth-owner scan: {e}")

        # Also reconcile owners already present in scheduled_tasks. This cleans
        # up stale/demo/deleted-user built-ins that are no longer in auth.json;
        # otherwise their old scheduled rows can keep firing forever.
        try:
            from core.database import SessionLocal, ScheduledTask
            from src.task_scheduler import HOUSEKEEPING_DEFAULTS
            builtin_names = []
            for defs in HOUSEKEEPING_DEFAULTS.values():
                builtin_names.append(defs["name"])
                builtin_names.extend(defs.get("legacy_names") or [])
            db_seed = SessionLocal()
            try:
                rows = db_seed.query(ScheduledTask.owner).filter(
                    (ScheduledTask.action.in_(list(HOUSEKEEPING_DEFAULTS.keys())))
                    | (ScheduledTask.name.in_(builtin_names))
                ).distinct().all()
                owners.update(row[0] for row in rows if row[0])
            finally:
                db_seed.close()
        except Exception as e:
            logger.debug(f"Default task existing-owner scan: {e}")

        try:
            for uname in sorted(owners):
                try:
                    await task_scheduler.ensure_defaults(uname)
                except Exception as e:
                    logger.debug(f"ensure_defaults({uname}): {e}")
        except Exception as e:
            logger.debug(f"Default tasks: {e}")

    # Reconcile built-in tasks before the runner starts. Otherwise legacy
    # scheduled built-ins can fire once before being converted to event tasks.
    await _ensure_default_tasks()

    # Disk-backed skills are not covered by the DB legacy-owner sweep. Repair
    # ownerless or deleted/test-owner SKILL.md files so strict owner filtering
    # does not make an existing library look empty after auth/account changes.
    try:
        import json as _json
        auth_path = os.path.join(DATA_DIR, "auth.json")
        with open(auth_path, encoding="utf-8") as f:
            users = _json.load(f).get("users", {})
        primary_owner = None
        for uname, udata in users.items():
            if udata.get("is_admin") is True:
                primary_owner = uname
                break
        if not primary_owner and users:
            primary_owner = next(iter(users))
        if primary_owner:
            changed = skills_manager.backfill_owner(primary_owner, set(users.keys()))
            if changed:
                logger.info("Assigned %s legacy skill file(s) to %s", changed, primary_owner)
    except Exception as e:
        logger.debug(f"Skill owner backfill skipped: {e}")

    # Start scheduled task runner — skip when running under a cron-driven
    # deployment where an external worker drives task firing. Mirrors
    # `PRISMA_INPROCESS_POLLERS` from the email pollers.
    _tasks_inprocess = os.environ.get("PRISMA_INPROCESS_TASKS", "1").strip().lower()
    if _tasks_inprocess not in ("0", "false", "no", "off", ""):
        await task_scheduler.start()
    else:
        logger.info(
            "In-process task scheduler disabled (PRISMA_INPROCESS_TASKS=0); "
            "drive task firing externally (e.g. cron)."
        )
    # Periodic null-owner sweep — re-runs the legacy-owner assignment hourly
    # so any data created while auth was disabled / localhost-bypassed gets
    # claimed by the admin instead of staying world-visible (M19).
    async def _null_owner_sweep_loop():
        while True:
            try:
                await asyncio.sleep(3600)
                from core.database import _migrate_assign_legacy_owner
                await asyncio.to_thread(_migrate_assign_legacy_owner)
            except Exception as e:
                logger.debug(f"Null-owner sweep skipped: {e}")
                await asyncio.sleep(3600)

    _startup_tasks.append(asyncio.create_task(_null_owner_sweep_loop()))

    # Nightly skill audit — at ~02:00 local, test + judge a batch of the
    # least-recently-checked skills, auto-fixing/escalating weak ones (never
    # deletes). Rotates through the library so each night covers different
    # skills. Gated by the `skill_audit_nightly` setting (default on); hour via
    # `skill_audit_hour` (default 2), batch size via `skill_audit_batch` (8).
    async def _skill_audit_nightly_loop():
        from datetime import timedelta
        while True:
            try:
                from src.settings import get_setting
                hour = int(get_setting("skill_audit_hour", 2) or 2)
            except Exception:
                hour = 2
            now = datetime.now()
            nxt = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(60, (nxt - now).total_seconds()))
            try:
                from src.settings import get_setting
                if not get_setting("skill_audit_nightly", True):
                    continue
                batch = int(get_setting("skill_audit_batch", 8) or 8)
                from routes.skills_routes import run_scheduled_skill_audit
                await run_scheduled_skill_audit(skills_manager, owner=None, max_skills=batch)
            except Exception as e:
                logger.warning(f"Nightly skill audit failed: {e}")

    _startup_tasks.append(asyncio.create_task(_skill_audit_nightly_loop()))
    logger.info("Application startup complete")

async def _shutdown_event():
    logger.info("Application shutting down...")
    if upload_cleanup_task:
        upload_cleanup_task.cancel()
        try:
            await upload_cleanup_task
        except asyncio.CancelledError:
            pass
    # Stop task scheduler (no-op if it never started under the gate)
    try:
        await task_scheduler.stop()
    except Exception:
        pass
    # Close webhook manager
    try:
        await webhook_manager.close()
    except Exception as e:
        logger.warning(f"Webhook manager shutdown error: {e}")
    # Disconnect all MCP servers
    try:
        await mcp_manager.disconnect_all()
    except Exception as e:
        logger.warning(f"MCP shutdown error: {e}")
    logger.info("Application shutdown complete")
