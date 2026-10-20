"""End-to-end contracts for Council per-request reasoning effort."""

from __future__ import annotations

import asyncio
from pathlib import Path

from src import llm_core
from src.council import orchestrator, seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat, SeatResult


async def _drain(cfg: CouncilConfig, **kwargs) -> list[dict]:
    async def collect():
        return [event async for event in run_council("reason carefully", cfg, **kwargs)]

    return await asyncio.wait_for(collect(), timeout=5)


def _layered_council() -> CouncilConfig:
    return CouncilConfig(seats=[
        Seat(id="panel", role="panelist", endpoint_id="e1", model="model", consumes=[]),
        Seat(id="aggregate", role="aggregator", endpoint_id="e1", model="model",
             consumes=["panel"]),
        Seat(id="chief", role="synthesizer", endpoint_id="e1", model="model",
             consumes=["aggregate"]),
    ])


async def test_orchestrator_threads_effort_to_batch_and_synthesis(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    async def fake_run_seat(seat, prompt, upstream, owner=None, registry=None,
                            reasoning_effort=None):
        calls.append((seat.id, reasoning_effort))
        return SeatResult(
            seat_id=seat.id, name=seat.name, role=seat.role, ok=True,
            content=f"answer from {seat.id}",
        )

    async def fake_stream_seat(seat, prompt, upstream, owner=None, registry=None,
                               reasoning_effort=None):
        calls.append((seat.id, reasoning_effort))
        yield "final answer"

    monkeypatch.setattr(orchestrator, "run_seat", fake_run_seat)
    monkeypatch.setattr(orchestrator, "stream_seat", fake_stream_seat)

    events = await _drain(_layered_council(), verify=False, reasoning_effort="high")

    assert calls == [("panel", "high"), ("aggregate", "high"), ("chief", "high")]
    assert events[-1]["type"] == "done"


async def test_orchestrator_threads_effort_to_repair_round(monkeypatch):
    calls: list[str | None] = []
    responses = iter([
        "```python\ndef broken(:\n    pass\n```",
        "```python\ndef fixed():\n    return True\n```",
    ])

    async def fake_stream_seat(seat, prompt, upstream, owner=None, registry=None,
                               reasoning_effort=None):
        calls.append(reasoning_effort)
        yield next(responses)

    monkeypatch.setattr(orchestrator, "stream_seat", fake_stream_seat)
    cfg = CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", endpoint_id="e1", model="model", consumes=[]),
    ])

    events = await _drain(cfg, reasoning_effort="max")

    assert calls == ["max", "max"]
    assert {event.get("stage") for event in events if event["type"] == "verification"} == {
        "initial", "repair",
    }


async def test_omitted_effort_preserves_legacy_seat_call_shape(monkeypatch):
    calls: list[str] = []

    async def legacy_stream_seat(seat, prompt, upstream, owner=None, registry=None):
        calls.append(seat.id)
        yield "answer"

    monkeypatch.setattr(orchestrator, "stream_seat", legacy_stream_seat)
    cfg = CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", endpoint_id="e1", model="model", consumes=[]),
    ])

    await _drain(cfg, verify=False)
    assert calls == ["chief"]


async def test_seat_runner_forwards_effort_to_both_llm_paths(monkeypatch):
    seat = Seat(id="chief", role="synthesizer", endpoint_id="e1", model="model", consumes=[])
    monkeypatch.setattr(
        seat_runner, "_resolve", lambda seat, owner: ("http://spark/v1/chat/completions", "model", {})
    )
    captured: dict[str, dict] = {}

    async def fake_call(url, model, messages, **kwargs):
        captured["call"] = kwargs
        return "complete"

    async def fake_stream(url, model, messages, **kwargs):
        captured["stream"] = kwargs
        yield 'data: {"delta":"streamed"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)
    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)

    result = await seat_runner.run_seat(
        seat, "prompt", [], owner="owner-1", reasoning_effort="low"
    )
    streamed = [delta async for delta in seat_runner.stream_seat(
        seat, "prompt", [], owner="owner-1", reasoning_effort="max"
    )]

    assert result.ok is True
    assert streamed == ["streamed"]
    assert captured["call"]["reasoning_effort"] == "low"
    assert captured["call"]["owner"] == "owner-1"
    assert captured["stream"]["reasoning_effort"] == "max"


def test_council_frontend_sends_existing_chatbox_effort():
    root = Path(__file__).resolve().parents[1]
    chat_js = (root / "static/js/chat.js").read_text(encoding="utf-8")
    council_js = (root / "static/js/council.js").read_text(encoding="utf-8")

    assert "getReasoningEffort," in chat_js
    assert "window.chatModule.getReasoningEffort()" in council_js
    assert "JSON.stringify({ prompt, reasoning_effort: reasoningEffort })" in council_js
