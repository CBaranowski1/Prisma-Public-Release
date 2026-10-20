"""Hermetic route-wiring tests for RMEAS-02 (Phase 40 Plan 02).

Proves (per 40-02-PLAN.md must_haves):
  - `RunRequest.escalate_model` exists, is `str | None`, and defaults to
    `None` (per-request, off by default -- no global setting);
  - the value genuinely flows through the `/run` route into the
    `run_council(...)` call, by spying on `routes.council_routes.run_council`
    and draining the route's `StreamingResponse` body iterator.

Offline / no-LLM / no-DB / no-network: the only seams patched are
`routes.council_routes.run_council` (spy) and `_load_council_config` /
`_select_council` (fixed offline stand-ins) so the handler never touches
per-user prefs storage.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import routes.council_routes as council_routes
from src.council.schemas import CouncilConfig, Seat


def _fake_request(owner: str = "u1") -> SimpleNamespace:
    """Minimal stand-in for a FastAPI Request -- only what the /run handler
    actually reads: request.state.current_user (via get_current_user) and
    request.app.state.memory_provider_registry."""
    return SimpleNamespace(
        state=SimpleNamespace(current_user=owner),
        app=SimpleNamespace(state=SimpleNamespace(memory_provider_registry=None)),
    )


def _get_run_endpoint():
    """Build the router and pull out the /run handler function directly --
    mirrors the direct-call precedent in test_council_dag_error_event.py,
    but one layer up (the route handler, not run_council itself)."""
    router = council_routes.setup_council_routes()
    run_route = next(r for r in router.routes if r.path.endswith("/run"))
    return run_route.endpoint


async def _drain(resp, timeout: float = 5.0) -> list[str]:
    """Run a StreamingResponse's lazy body_iterator to completion (the
    event_stream() async generator isn't executed until iterated)."""
    async def _collect():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return chunks
    return await asyncio.wait_for(_collect(), timeout=timeout)


def test_run_request_has_escalate_model_field_default_none():
    field = council_routes.RunRequest.model_fields["escalate_model"]
    assert field.default is None, (
        "escalate_model must default to None (per-request, off by default)"
    )


async def test_route_threads_escalate_model_into_run_council(monkeypatch):
    monkeypatch.setattr(
        council_routes, "_load_council_config", lambda owner: {"councils": [{"id": "c1"}]}
    )
    fixed_council = CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", model="weak-model",
             endpoint_id="e1", consumes=[]),
    ])
    monkeypatch.setattr(
        council_routes, "_select_council", lambda cfg, council_id: fixed_council
    )

    captured: dict = {}

    async def fake_run_council(prompt, cfg, owner=None, registry=None,
                                verify_execute=False, fresh_context=True,
                                escalate_model=None, reasoning_effort=None):
        captured["escalate_model"] = escalate_model
        captured["reasoning_effort"] = reasoning_effort
        captured["verify_execute"] = verify_execute
        captured["fresh_context"] = fresh_context
        yield {"type": "done", "final": prompt, "verified": True, "verification": None}

    monkeypatch.setattr(council_routes, "run_council", fake_run_council)

    run_fn = _get_run_endpoint()
    body = council_routes.RunRequest(prompt="hello council", escalate_model="strong-model")
    resp = await run_fn(_fake_request(), body)

    await _drain(resp)

    assert captured["escalate_model"] == "strong-model", (
        "escalate_model on the request body must reach run_council unchanged"
    )


async def test_omitting_escalate_model_passes_none_through(monkeypatch):
    """Byte-for-byte parity companion: omitting escalate_model on the request
    must reach run_council as None, not '' or some other sentinel."""
    monkeypatch.setattr(
        council_routes, "_load_council_config", lambda owner: {"councils": [{"id": "c1"}]}
    )
    fixed_council = CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", model="weak-model",
             endpoint_id="e1", consumes=[]),
    ])
    monkeypatch.setattr(
        council_routes, "_select_council", lambda cfg, council_id: fixed_council
    )

    captured: dict = {}

    async def fake_run_council(prompt, cfg, owner=None, registry=None,
                                verify_execute=False, fresh_context=True,
                                escalate_model=None, reasoning_effort=None):
        captured["escalate_model"] = escalate_model
        captured["reasoning_effort"] = reasoning_effort
        yield {"type": "done", "final": prompt, "verified": True, "verification": None}

    monkeypatch.setattr(council_routes, "run_council", fake_run_council)

    run_fn = _get_run_endpoint()
    body = council_routes.RunRequest(prompt="hello council")  # escalate_model omitted
    resp = await run_fn(_fake_request(), body)

    await _drain(resp)

    assert captured["escalate_model"] is None
    assert captured["reasoning_effort"] is None


async def test_route_normalizes_and_threads_reasoning_effort(monkeypatch):
    monkeypatch.setattr(
        council_routes, "_load_council_config", lambda owner: {"councils": [{"id": "c1"}]}
    )
    fixed_council = CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", model="model",
             endpoint_id="e1", consumes=[]),
    ])
    monkeypatch.setattr(
        council_routes, "_select_council", lambda cfg, council_id: fixed_council
    )
    captured: dict = {}

    async def fake_run_council(prompt, cfg, **kwargs):
        captured.update(kwargs)
        yield {"type": "done", "final": prompt, "verified": True, "verification": None}

    monkeypatch.setattr(council_routes, "run_council", fake_run_council)
    run_fn = _get_run_endpoint()

    explicit = council_routes.RunRequest(prompt="hello", reasoning_effort=" HIGH ")
    resp = await run_fn(_fake_request(), explicit)
    await _drain(resp)
    assert captured["reasoning_effort"] == "high"

    automatic = council_routes.RunRequest(prompt="hello", reasoning_effort="auto")
    resp = await run_fn(_fake_request(), automatic)
    await _drain(resp)
    assert captured["reasoning_effort"] == "high"

    invalid = council_routes.RunRequest(prompt="hello", reasoning_effort="turbo")
    resp = await run_fn(_fake_request(), invalid)
    await _drain(resp)
    assert captured["reasoning_effort"] is None
