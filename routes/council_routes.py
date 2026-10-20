# routes/council_routes.py
"""Council feature routes: per-user config CRUD + a streaming deliberation run."""

import copy
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError, field_validator

from src.auth_helpers import get_current_user
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig
from src.settings import DEFAULT_COUNCIL, get_setting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/council", tags=["council"])

_COUNCIL_KEY = "council"


def _load_council_config(owner) -> dict:
    """Per-user council config, falling back to the seeded global default."""
    try:
        from routes.prefs_routes import _load_for_user
        prefs = _load_for_user(owner) or {}
        cfg = prefs.get(_COUNCIL_KEY)
        if isinstance(cfg, dict) and cfg.get("councils"):
            return cfg
    except Exception:
        logger.debug("council: per-user prefs unavailable; using global", exc_info=True)
    cfg = get_setting(_COUNCIL_KEY, DEFAULT_COUNCIL)
    if isinstance(cfg, dict) and cfg.get("councils"):
        return copy.deepcopy(cfg)
    return copy.deepcopy(DEFAULT_COUNCIL)


def _save_council_config(owner, cfg: dict):
    from routes.prefs_routes import _load_for_user, _save_for_user
    prefs = _load_for_user(owner) or {}
    prefs[_COUNCIL_KEY] = cfg
    _save_for_user(owner, prefs)


def _select_council(cfg: dict, council_id: str | None) -> CouncilConfig:
    """Pick the requested (or default) council and validate it into a model."""
    councils = cfg.get("councils") or []
    if not councils:
        raise HTTPException(400, "no councils configured")
    target = council_id or cfg.get("default_council_id")
    chosen = next((c for c in councils if c.get("id") == target), councils[0])
    try:
        return CouncilConfig(**chosen)
    except ValidationError as e:
        raise HTTPException(400, f"invalid council config: {e}") from e


class RunRequest(BaseModel):
    prompt: str
    council_id: str | None = None
    # Uses the same chatbox control as normal Chat turns. ``auto`` resolves to
    # high for Council because every Council run is a multi-agent reasoning
    # workflow; omitting the field preserves the endpoint/model default.
    reasoning_effort: str | None = None
    # Verification gate. Syntax check + one repair round are on by default (safe,
    # no code execution). Set verify_execute=True to also RUN candidate code in a
    # sandbox before the answer is emitted — the full anti-hallucination pipeline.
    verify_execute: bool = False
    # Fresh-context (GSD-ported): curate each seat's upstream context to a lean
    # window instead of dumping every upstream answer in full — fights context rot.
    fresh_context: bool = True
    # Escalation (per-request, opt-in): swap the existing verify-gate repair
    # round's synthesizer to a stronger model. Off by default, no global
    # setting — validated by run_council's normalize_model_id gate.
    escalate_model: str | None = None

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value):
        if value is None:
            return None
        normalized = str(value).strip().lower()
        return normalized if normalized in {"auto", "low", "high", "max"} else None


def _effective_reasoning_effort(requested: str | None) -> str | None:
    """Resolve Council's UI-level effort without changing legacy API calls."""
    if requested == "auto":
        return "high"
    return requested


def setup_council_routes(session_manager=None):
    """Build the council router. `session_manager` accepted for parity with the
    other route factories (compare/session) though not currently needed."""

    @router.get("/config")
    async def get_config(request: Request):
        owner = get_current_user(request)
        return _load_council_config(owner)

    @router.put("/config")
    async def put_config(request: Request, body: dict):
        owner = get_current_user(request)
        councils = body.get("councils")
        if not isinstance(councils, list) or not councils:
            raise HTTPException(400, "config must include a non-empty 'councils' list")
        for c in councils:
            try:
                CouncilConfig(**c)
            except ValidationError as e:
                raise HTTPException(400, f"invalid council '{c.get('id', '?')}': {e}") from e
        cfg = {
            "name": body.get("name", "The Council"),
            "enabled": bool(body.get("enabled", True)),
            "default_council_id": body.get("default_council_id")
            or councils[0].get("id"),
            "councils": councils,
        }
        _save_council_config(owner, cfg)
        return cfg

    @router.post("/run")
    async def run(request: Request, body: RunRequest):
        owner = get_current_user(request)
        prompt = (body.prompt or "").strip()
        if not prompt:
            raise HTTPException(400, "prompt is required")
        cfg = _load_council_config(owner)
        council = _select_council(cfg, body.council_id)

        registry = getattr(request.app.state, "memory_provider_registry", None)

        async def event_stream():
            try:
                async for event in run_council(prompt, council, owner=owner,
                                               registry=registry,
                                               verify_execute=bool(body.verify_execute),
                                               fresh_context=bool(body.fresh_context),
                                               escalate_model=(body.escalate_model or None),
                                               reasoning_effort=_effective_reasoning_effort(
                                                   body.reasoning_effort
                                               )):
                    yield f"data: {json.dumps(event)}\n\n"
            except Exception as e:  # surface, don't 500 mid-stream
                logger.exception("council run failed")
                yield f'data: {json.dumps({"type": "error", "error": str(e)})}\n\n'
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    return router
