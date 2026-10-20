"""Gated answer endpoint: POST /api/answer.

The offline model answers ONLY through the pipeline — mandatory brain retrieval +
a VERA reviewer agent that iterates with the brain to consensus. Agents are not a
user-facing chat partner here; they are internal hallucination mitigation.
"""
from __future__ import annotations
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api", tags=["answer"])


class AnswerRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20000)
    model: str = Field(default="", max_length=300)
    max_rounds: int = Field(default=3, ge=1, le=6)
    reviewer_model: str = Field(default="", max_length=300)
    escalate_model: str = Field(default="", max_length=300)
    escalate_after: int = Field(default=1, ge=0, le=6)
    # Orchestration (opt-in; defaults preserve current single-pass behavior).
    samples: int = Field(default=1, ge=1, le=9)          # self-consistency: N drafts -> vote
    structured: bool = Field(default=False)              # plan -> execute -> verify gate
    # Sandbox code gate: test the answer's OWN code before approving it.
    code_verify: bool = Field(default=False)             # syntax-check code + one repair round
    code_execute: bool = Field(default=False)            # also RUN the code in a sandbox
    # Fresh-context budget guard override (opt-in on the API; run_pipeline
    # itself defaults ON to DEFAULT_WINDOW as of Phase 8, D-02). None/omitted
    # here means "use run_pipeline's default-on behavior" -- see the handler's
    # CONDITIONAL forward below. Bounded (T-08-04, DoS mitigation): ge=0 lets
    # an explicit 0 disable curation via run_pipeline's truthiness gate,
    # le=200000 caps the oversized-prompt/cost vector.
    context_window: int | None = Field(default=None, ge=0, le=200000)


@router.post("/answer")
async def answer(request: Request, body: AnswerRequest) -> dict:
    from src.answer_pipeline import run_pipeline, wire_defaults
    from src.dgx_spark import load_dgx_spark_config

    registry = getattr(request.app.state, "memory_provider_registry", None)
    dgx = load_dgx_spark_config(require_model=True)
    model = body.model or (dgx.model if dgx is not None else "vera-coder")
    reviewer_model = body.reviewer_model or model
    wire_kwargs = {
        "reviewer_model": reviewer_model,
        "escalate_model": body.escalate_model or None,
        "owner": getattr(request.state, "current_user", None),
    }
    if dgx is not None and model == dgx.model:
        wire_kwargs["ollama_url"] = dgx.base_url
        wire_kwargs["request_headers"] = dgx.headers
    fns = wire_defaults(model, registry, **wire_kwargs)
    # BUILDER TRAP (Phase 8, D-02): forward context_window CONDITIONALLY. An
    # unconditional `context_window=body.context_window` would forward `None`
    # whenever the field is omitted, re-disabling curation and defeating the
    # run_pipeline default-on flip above. Only pass the kwarg when the caller
    # actually set it, so omitted/None falls through to run_pipeline's default.
    extra_kwargs = {}
    if body.context_window is not None:
        extra_kwargs["context_window"] = body.context_window
    r = run_pipeline(body.prompt, max_rounds=body.max_rounds, escalate_after=body.escalate_after,
                     samples=body.samples, structured=body.structured,
                     code_verify=body.code_verify, code_execute=body.code_execute,
                     **extra_kwargs, **fns)
    return {"answer": r.answer, "approved": r.approved, "rounds": r.rounds,
            "grounded": r.grounded, "sources": r.sources, "transcript": r.transcript,
            "orchestration": r.orchestration, "provenance": r.provenance}
