"""OpenAI-compatible facade — /v1/models + /v1/chat/completions.

Thin authenticating proxy so external OpenAI clients can reach Prisma's
configured upstreams. Token-gated on PRISM_TOKEN.
"""
import logging
import secrets
from typing import AsyncIterator, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.constants import PRISM_TOKEN as _PRISM_TOKEN  # monkeypatched in tests
from src.endpoint_resolver import build_chat_url, normalize_base

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OpenAI error-type mapping
# ---------------------------------------------------------------------------

_HTTP_STATUS_TO_OAI_TYPE: dict[int, str] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "server_error",
}


def _oai_error(status: int, message: str, error_type: Optional[str] = None) -> JSONResponse:
    """Return a JSONResponse with an OpenAI-shaped error body."""
    etype = error_type or _HTTP_STATUS_TO_OAI_TYPE.get(status, "server_error")
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": etype}},
    )


# ---------------------------------------------------------------------------
# Thin module-level wrappers — monkeypatched in tests
# ---------------------------------------------------------------------------

def _resolve_model(spec: str, owner: Optional[str] = None):
    """Delegate to src.ai_interaction._resolve_model → (upstream_url, model_id, headers)."""
    from src.ai_interaction import _resolve_model as _real
    return _real(spec, owner=owner)


def _detect_provider(base: str) -> str:
    """Delegate to src.llm_core._detect_provider."""
    from src.llm_core import _detect_provider as _real
    return _real(base)


async def _proxy_stream(url: str, headers: dict, body: dict) -> AsyncIterator[bytes]:
    """Stream a POST request to *url* and yield raw response bytes."""
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            async for chunk in resp.aiter_raw():
                yield chunk


async def _proxy_json(
    url: str,
    headers: dict,
    body: dict,
    *,
    timeout: float = 120,
):
    """Non-streaming POST to *url*; returns the parsed JSON response body.

    Raises ``httpx.HTTPStatusError`` on non-2xx so the caller can surface it
    as an OpenAI-shaped error rather than an unhandled 500.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()


def _check_bearer(request: Request) -> None:
    if not _PRISM_TOKEN:
        raise HTTPException(503, detail="OpenAI facade not configured (PRISM_TOKEN not set)")
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token or not secrets.compare_digest(token, _PRISM_TOKEN):
        raise HTTPException(401, detail="Invalid or missing bearer token")


def _visible_model_ids(owner: Optional[str]) -> list[str]:
    """Flat, deduped, sorted list of chat-capable model ids visible to `owner`.

    Reuses the REAL module-level helpers from routes/model_routes.py
    (all importable — confirmed: `_cached_model_ids`, `_visible_models`,
    `_is_chat_model` are module-level functions, not closures).
    """
    from core.database import SessionLocal, ModelEndpoint
    from src.auth_helpers import owner_filter
    from routes.model_routes import _cached_model_ids, _visible_models, _is_chat_model

    ids: set[str] = set()
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if owner:
            q = owner_filter(q, ModelEndpoint, owner)
        for ep in q.all():
            # Skip non-LLM endpoints (image/embedding) — chat facade only.
            if (getattr(ep, "model_type", None) or "llm") != "llm":
                continue
            visible = _visible_models(
                _cached_model_ids(ep),
                getattr(ep, "hidden_models", None),
                getattr(ep, "pinned_models", None),
            )
            ids.update(m for m in visible if _is_chat_model(m))
    finally:
        db.close()
    return sorted(ids)


def _resolve_owner(request: Request) -> Optional[str]:
    """Owner scope for token-authenticated facade calls.

    For the desktop bridge the PRISM_TOKEN identifies the single desktop user;
    return "" (single-user / see-all) unless a PRISM_OWNER mapping is configured.
    FLAG: confirm desired owner-scoping with the real auth model before finalizing.
    """
    return ""


def register_exception_handler(app) -> None:
    """Register the OpenAI-shaped HTTPException handler on a FastAPI *app*.

    Call this from the main application after ``include_router`` so that any
    HTTPException raised inside /v1/* routes is rendered as an OpenAI error
    body.  The routes themselves also return ``_oai_error`` directly (belt &
    suspenders) so the tests don't need the global handler.
    """
    from fastapi import FastAPI
    from fastapi.exception_handlers import http_exception_handler as _default

    @app.exception_handler(HTTPException)
    async def _oai_http_exception_handler(request: Request, exc: HTTPException):
        return _oai_error(exc.status_code, str(exc.detail))


def setup_openai_compat_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    async def list_models(request: Request):
        if not _PRISM_TOKEN:
            return _oai_error(503, "OpenAI facade not configured (PRISM_TOKEN not set)", "server_error")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token or not secrets.compare_digest(token, _PRISM_TOKEN):
            return _oai_error(401, "Invalid or missing bearer token", "authentication_error")
        owner = _resolve_owner(request)
        data = [
            {"id": mid, "object": "model", "owned_by": "prism"}
            for mid in _visible_model_ids(owner)
        ]
        return {"object": "list", "data": data}

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        # Auth check — return OpenAI-shaped errors directly
        if not _PRISM_TOKEN:
            return _oai_error(503, "OpenAI facade not configured (PRISM_TOKEN not set)", "server_error")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token or not secrets.compare_digest(token, _PRISM_TOKEN):
            return _oai_error(401, "Invalid or missing bearer token", "authentication_error")

        body = await request.json()

        # Carry-forward #4: guard missing `model` field
        if not body.get("model"):
            return _oai_error(400, "missing required field: model", "invalid_request_error")

        spec = body.get("model", "")
        owner = _resolve_owner(request)

        try:
            upstream, model_id, up_headers = _resolve_model(spec, owner)
        except ValueError as exc:
            return _oai_error(404, str(exc), "invalid_request_error")

        base = normalize_base(upstream)
        if _detect_provider(base) == "anthropic":
            return _oai_error(400, "model's upstream is not OpenAI-compatible", "invalid_request_error")

        body = dict(body)  # shallow copy — don't mutate caller's dict
        body["model"] = model_id
        chat_url = build_chat_url(base)

        if body.get("stream"):
            return StreamingResponse(
                _proxy_stream(chat_url, up_headers, body),
                media_type="text/event-stream",
            )

        # Non-streaming path — carry-forward #3: surface upstream errors as
        # OpenAI-shaped responses rather than unhandled 500s.
        try:
            result = await _proxy_json(chat_url, up_headers, body)
        except httpx.HTTPStatusError as exc:
            upstream_status = exc.response.status_code
            # Try to relay the upstream error body; fall back to a generic message.
            try:
                upstream_body = exc.response.json()
                message = (
                    upstream_body.get("error", {}).get("message")
                    or upstream_body.get("message")
                    or str(exc)
                )
            except Exception:
                message = f"upstream error: HTTP {upstream_status}"
            # Surface as 502 so the client knows it's a gateway-level problem.
            return _oai_error(502, message, "server_error")
        except Exception as exc:
            logger.exception("Unexpected error proxying non-streaming request")
            return _oai_error(502, f"upstream connection error: {exc}", "server_error")

        return JSONResponse(content=result)

    return router
