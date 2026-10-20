"""tests/test_deferred_smoke_p29_escalation_council.py — Phase 37 Plan 04
(SMOKE-01): automated re-encoding of Phase 29's deferred live smoke
(29-04-PLAN.md Task 2, recorded as `human_verification` in
29-VERIFICATION.md).

That deferred item asked a human to confirm, on a running server with a real
model endpoint: (1) an unresolvable `agent_escalation_model` surfaces
`model_escalation_failed` with no swap and the turn continues; (2) a
resolvable escalation model still swaps; (3) a cyclic/malformed council seat
graph emits an `error` event with no 500/stack trace; (4) a wedged
synthesizer degrades to a timeout event + an honest partial `done`; (5)
standard council + answer flows are unchanged with `fresh_context` OFF.

This file proves all 5 properties at the assembled-generator layer,
hermetically (no live LLM/DB/network — every async drive is bounded by an
outer `asyncio.wait_for(timeout=15)` so a hang/timeout regression fails LOUD
in CI instead of wedging the suite). It elevates ABOVE
`tests/test_phase29_integration.py` (SC-5) rather than duplicating it:
SC-5 proves properties 1 and 3 with a single fixture each; this file adds
property 2 (the resolvable-swap counterpart), property 5 (flag-off parity
across BOTH the council and answer paths, plus the Council non-primitive
schema assertion), and property 4 (synthesizer-timeout partial-done), which
SC-5 does not cover at all.

RESIDUAL (documented, non-blocking, per this plan's success criteria): the
real SSE transport (routes/council_routes.py, routes/chat_routes.py) and a
real reachable model endpoint are NOT exercised here — this suite drives the
assembled generators (`stream_agent_loop`, `run_council`) directly, the same
seams `tests/test_phase29_integration.py`, `tests/test_escalation_resolve.py`,
and `tests/test_council_timeouts.py` already reuse. This is the documented,
non-blocking residual named in 37-04-PLAN.md's success criteria — it does
not reopen the human-verification item Phase 29 deferred.

The Council escalation primitive is now BUILT (Phase 40 / RMEAS-02) at the
orchestrator repair-round level (`run_council`'s `escalate_model` param,
src/council/orchestrator.py — see tests/test_council_escalation.py and
tests/test_escalation_resolve.py for the presence/behavioral proofs). Test C
below asserts the invariant this still preserves: escalation is deliberately
NOT a Seat field — it's an orchestrator-level `model_copy` override, per
ARCHITECTURE.
"""
from __future__ import annotations

import asyncio
import json

import httpx

import src.answer_pipeline as answer_pipeline
import src.council.orchestrator as orch
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat

from tests.test_agent_loop_verify_gate import (
    agent_loop,
    settings_module,
    tool_index_module,
    _make_fake_get_setting,
    _make_fake_stream,
    _tool_call_chunks,
    _delta_chunks,
    _typed_events,
)
from tests.test_council_fresh_context_integration import _make_stubs

PROMPT = "What should we do?"


def _two_layer_cfg() -> CouncilConfig:
    """One panelist consumed by one synthesizer — minimal standard council."""
    return CouncilConfig(seats=[
        Seat(id="p1", role="panelist", consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["p1"]),
    ])


async def _run_council_bounded(cfg: CouncilConfig, prompt: str = PROMPT, **kw) -> list[dict]:
    """Drain run_council into a plain list, under a hang-safety ceiling."""
    async def _consume():
        out = []
        async for ev in run_council(prompt, cfg, **kw):
            out.append(ev)
        return out

    return await asyncio.wait_for(_consume(), timeout=15)


# ---------------------------------------------------------------------------
# Chat-half (agent_loop._escalate) driver — mirrors
# tests/test_escalation_resolve.py's `_drive_agent_loop`, additionally
# capturing the MODEL used on every round (no existing SSE event otherwise
# exposes whether `_escalate()` actually swapped the model). Drives the
# ASSEMBLED `stream_agent_loop` path, never mocks `_escalate` itself.
# ---------------------------------------------------------------------------
def _make_fake_normalize(resolved_id, call_log=None):
    """Fake `normalize_model_id(endpoint_url, requested, timeout=...)`.

    Returns `resolved_id` (a truthy resolved model id, or None to script an
    unresolvable target) and records every call in `call_log` if given.
    """

    def fake_normalize_model_id(endpoint_url, requested, timeout=None):
        if call_log is not None:
            call_log.append((endpoint_url, requested, timeout))
        return resolved_id

    return fake_normalize_model_id


async def _drive_agent_loop_escalation(monkeypatch, rounds, *, settings=None,
                                        model_log=None, **loop_kwargs):
    fake_get_setting = _make_fake_get_setting(settings)
    _base_fake_stream = _make_fake_stream(rounds)

    async def _capturing_fake_stream(candidates, messages, *args, **kwargs):
        if model_log is not None:
            model_log.append(candidates[0][1])
        async for chunk in _base_fake_stream(candidates, messages, *args, **kwargs):
            yield chunk

    async def _fake_execute_tool_block(block, session_id=None, disabled_tools=None,
                                        owner=None, progress_cb=None):
        return "tool executed (stub)", {"exit_code": 0, "output": ""}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", _capturing_fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", _fake_execute_tool_block)
    monkeypatch.setattr(agent_loop, "get_setting", fake_get_setting)
    monkeypatch.setattr(settings_module, "get_setting", fake_get_setting)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(tool_index_module, "get_tool_index", lambda: None)

    default_kwargs = dict(
        endpoint_url="http://localhost:11434",
        model="test-model",
        messages=[{"role": "user", "content": "go"}],
        owner="owner-1",
        session_id="sess-37-04",
        max_rounds=len(rounds),
    )
    default_kwargs.update(loop_kwargs)

    async def _consume():
        out = []
        async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
            out.append(chunk)
        return out

    # Outer ceiling: a hang/timeout regression must fail LOUD, never wedge CI.
    return await asyncio.wait_for(_consume(), timeout=15)


def _escalation_rounds():
    # Round 1 fires a tool call (keeps the loop going, triggers the
    # round-budget escalation check before round 2's LLM call); round 2 is
    # the plain-prose final answer.
    return [
        _tool_call_chunks("read_file", {"path": "foo.txt"}),
        _delta_chunks("final answer"),
    ]


# ---------------------------------------------------------------------------
# Test A: escalation-failed — unresolvable target, no swap, turn continues.
# ---------------------------------------------------------------------------
async def test_unresolvable_escalation_emits_failed_event_no_swap_turn_continues(monkeypatch):
    call_log = []
    monkeypatch.setattr(
        agent_loop, "normalize_model_id",
        _make_fake_normalize(None, call_log),
        raising=False,
    )

    model_log = []
    events = await _drive_agent_loop_escalation(
        monkeypatch, _escalation_rounds(),
        settings={"agent_escalation_model": "unresolvable-dead-model"},
        model_log=model_log,
        max_rounds=2,
    )

    # The resolve-gate seam was actually exercised...
    assert len(call_log) == 1
    assert call_log[0][:2] == ("http://localhost:11434", "unresolvable-dead-model")

    # ...the model used on EVERY round is unchanged — no swap into the
    # unresolvable target ever happens.
    assert model_log == ["test-model", "test-model"]

    # ...the assembled stream surfaces the bounded failure event (never a
    # model_escalation swap event)...
    failed = _typed_events(events, "model_escalation_failed")
    assert len(failed) == 1
    assert failed[0]["target"] == "unresolvable-dead-model"
    assert "reason" in failed[0]
    assert not _typed_events(events, "model_escalation")

    # ...and the turn still completes rather than aborting.
    assert "final answer" in "".join(events)


# ---------------------------------------------------------------------------
# Test B: resolvable swap — a resolvable target DOES swap normally.
# ---------------------------------------------------------------------------
async def test_resolvable_escalation_target_swaps_model(monkeypatch):
    call_log = []
    monkeypatch.setattr(
        agent_loop, "normalize_model_id",
        _make_fake_normalize("big-model-resolved", call_log),
        raising=False,
    )

    model_log = []
    events = await _drive_agent_loop_escalation(
        monkeypatch, _escalation_rounds(),
        settings={"agent_escalation_model": "big-model"},
        model_log=model_log,
        max_rounds=2,
    )

    # Round 1 uses the original model; round 2 (after the gated resolve
    # check succeeds) uses the resolved escalation target.
    assert model_log == ["test-model", "big-model-resolved"]
    assert len(call_log) == 1
    assert call_log[0][:2] == ("http://localhost:11434", "big-model")

    esc_events = _typed_events(events, "model_escalation")
    assert len(esc_events) == 1
    assert esc_events[0]["from"] == "test-model"
    assert esc_events[0]["to"] == "big-model-resolved"
    assert not _typed_events(events, "model_escalation_failed")
    assert "final answer" in "".join(events)


# ---------------------------------------------------------------------------
# Test C: flag-off parity — standard council + answer flows unchanged with
# fresh_context OFF / no escalate_model, plus the Council non-primitive
# schema assertion (documenting the deliberate absence, not building one).
# ---------------------------------------------------------------------------
async def test_standard_council_flow_fresh_context_off_completes_without_escalation(monkeypatch):
    cfg = _two_layer_cfg()
    _make_stubs(
        monkeypatch,
        scripted={"p1": ["p1 answer, short and simple."],
                  "chief": ["final synth answer"]},
    )

    events = await _run_council_bounded(cfg, fresh_context=False)

    # Standard flow completes normally — no error events, ends in `done`.
    # (run_council already yields plain dicts, not SSE-framed strings, so
    # filter directly rather than via the SSE-string-parsing _typed_events.)
    assert not [e for e in events if e["type"] == "error"]
    done = next(e for e in events if e["type"] == "done")
    assert done["final"].strip() == "final synth answer"
    # Council has no escalation event vocabulary at all — the standard flow
    # never fires one, flag on or off.
    assert not any("escalat" in e.get("type", "") for e in events)


def test_answer_author_fn_no_escalate_is_byte_identical(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["model"] = json["model"]
        captured["messages"] = json["messages"]

        class _FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "a thorough answer"}}]}

        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    def _raise_if_called(*args, **kwargs):
        raise AssertionError(
            "normalize_model_id must NEVER be called when escalate_model is "
            "falsy — byte-identical no-escalation contract violated"
        )

    monkeypatch.setattr(
        answer_pipeline, "normalize_model_id", _raise_if_called, raising=False,
    )

    # No escalate_model at all — the standard, no-escalation Answer path.
    fns = answer_pipeline.wire_defaults("test-model", registry=None)
    out = fns["author_fn"]("some draft prompt")

    assert out == "a thorough answer"
    assert captured["model"] == "test-model"


def test_council_seat_has_no_escalation_or_swap_field():
    field_names = set(Seat.model_fields.keys())
    escalation_like = {
        name for name in field_names
        if "escalat" in name.lower() or "fallback_model" in name.lower()
    }
    assert escalation_like == set(), (
        f"Seat gained an escalation-like field {escalation_like!r} — the "
        "Council escalation primitive is now BUILT (Phase 40 / RMEAS-02) at "
        "the orchestrator repair-round level; this invariant asserts it "
        "deliberately did NOT add a Seat field (escalation is a model_copy "
        "override, per ARCHITECTURE)."
    )


# ---------------------------------------------------------------------------
# Test D: cyclic/malformed DAG -> a SINGLE error event, no exception escapes
# the async generator. Driven DIRECTLY against run_council (never via
# routes/council_routes.py's own blanket except) — proves the fix lives
# inside run_council itself. Note: `CouncilConfig.active_seats()` prunes a
# `consumes` id that isn't in the live seat set (schemas.py:54-62 — a
# dangling reference is deliberately safe-by-design, not an error), so the
# only way to reach `layers()`'s ValueError is a genuine cycle among enabled
# seats. Uses a 3-seat cycle (a->b->c->a) rather than
# tests/test_phase29_integration.py's direct 2-cycle, to prove the contained
# behavior holds for a deeper malformed graph too, not just the minimal case.
# ---------------------------------------------------------------------------
def _three_seat_cycle_cfg() -> CouncilConfig:
    """Three seats in a cycle: a consumes b, b consumes c, c consumes a —
    `layers()` can never make progress and must raise ValueError."""
    return CouncilConfig(seats=[
        Seat(id="a", role="panelist", consumes=["b"]),
        Seat(id="b", role="panelist", consumes=["c"]),
        Seat(id="c", role="synthesizer", consumes=["a"]),
    ])


async def test_cyclic_or_malformed_dag_yields_single_error_event_no_raise(monkeypatch):
    _make_stubs(monkeypatch)

    events = await _run_council_bounded(_three_seat_cycle_cfg())

    assert events, "run_council must yield at least one event"
    assert events[0]["type"] == "error"
    err = events[0]["error"].lower()
    assert "cycle" in err or "missing dependency" in err
    # Generator ends cleanly right after the error event — no exception
    # escaped the async generator, and no run_started followed it (the
    # generator returned immediately after the sole error event).
    assert len(events) == 1


async def test_direct_cycle_dag_also_yields_single_error_event_no_raise(monkeypatch):
    """The direct 2-cycle shape (a consumes b, b consumes a) — same
    ValueError path, different malformed-graph cause, same contained
    behavior."""
    _make_stubs(monkeypatch)
    cyclic_cfg = CouncilConfig(seats=[
        Seat(id="a", role="panelist", consumes=["b"]),
        Seat(id="b", role="synthesizer", consumes=["a"]),
    ])

    events = await _run_council_bounded(cyclic_cfg)

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert "cycle" in events[0]["error"].lower() or "missing dependency" in events[0]["error"].lower()


# ---------------------------------------------------------------------------
# Test E: a wedged synthesizer degrades to a timeout AND the run still
# finalizes an honest partial `done` — the deadline break falls through to
# the finalize path, not an early return. Monkeypatches
# orchestrator.SYNTH_TIMEOUT_S to a small value and scripts a stream_llm
# that sleeps well past the deadline (mirrors
# tests/test_council_timeouts.py's canonical fixture, driven here as part of
# this plan's own consolidated smoke).
# ---------------------------------------------------------------------------
def _patch_slow_stream_llm(monkeypatch, delay: float):
    """Replace llm_core.stream_llm with a generator that sleeps `delay`
    seconds (longer than the timeout under test) before yielding anything —
    simulates a wedged synthesizer stream."""
    async def fake_slow_stream_llm(url, model, messages, temperature=1.0, headers=None, **kw):
        await asyncio.sleep(delay)
        yield f"data: {json.dumps({'delta': 'too late'})}\n\n"
        yield "data: [DONE]\n\n"

    import src.llm_core as llm_core_mod
    monkeypatch.setattr(llm_core_mod, "stream_llm", fake_slow_stream_llm)


async def test_wedged_synthesizer_degrades_to_timeout_and_finalizes_partial_done(monkeypatch):
    monkeypatch.setattr(orch, "SYNTH_TIMEOUT_S", 0.05)
    _make_stubs(monkeypatch)
    _patch_slow_stream_llm(monkeypatch, delay=1.0)

    events = await _run_council_bounded(_two_layer_cfg())

    timeout_events = [
        e for e in events
        if e.get("type") == "seat_status" and e.get("status") == "error"
        and e.get("error") == "timeout"
    ]
    assert timeout_events, f"expected a timeout seat_status event, got: {events}"
    # The deadline break falls through to the finalize path — the run still
    # ends in an honest `done` event, never a silent drop or hang.
    assert events[-1]["type"] == "done"
