"""tests/test_council_timeouts.py — GRAY-AGT-06 (Phase 29 Plan 02).

Proves:
  - both stream_seat consumption sites in run_council (initial synthesis AND
    verify-repair) are bounded by a wall-clock SYNTH_TIMEOUT_S deadline: a
    wedged/slow synthesizer stream degrades to a {"type": "seat_status",
    "status": "error", "error": "timeout"} event and the run still reaches a
    `done` event -- never hangs.
  - a healthy fast synthesizer is unaffected (no spurious timeout).
  - the overall council-run wall-clock (COUNCIL_RUN_TIMEOUT_S) breaches emit
    a run-level {"type": "error", ...} event, break the layer loop, and STILL
    finalize an honest partial `done` event -- never an early return/hang.
  - an under-budget run is unaffected (no run-level error event).

Offline only: reuses tests/test_council_fresh_context_integration.py's
_make_stubs seam (patches the LLM boundary + the DB resolver), and
monkeypatches src.council.orchestrator's SYNTH_TIMEOUT_S / COUNCIL_RUN_TIMEOUT_S
module constants to tiny values so slow-path tests run fast. No live LLM/
DB/network is ever touched.
"""
from __future__ import annotations

import asyncio
import json

import src.council.orchestrator as orch
from src.council.schemas import CouncilConfig, Seat
from tests.test_council_fresh_context_integration import PROMPT, _make_stubs, _run


def _two_layer_cfg() -> CouncilConfig:
    """One panelist consumed by one synthesizer."""
    return CouncilConfig(seats=[
        Seat(id="p1", role="panelist", consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["p1"]),
    ])


def _patch_slow_stream_llm(monkeypatch, delay: float):
    """Replace llm_core.stream_llm with a generator that sleeps `delay`
    seconds (longer than the timeout under test) before yielding anything --
    simulates a wedged synthesizer stream."""
    async def fake_slow_stream_llm(url, model, messages, temperature=1.0, headers=None, **kw):
        await asyncio.sleep(delay)
        yield f"data: {json.dumps({'delta': 'too late'})}\n\n"
        yield "data: [DONE]\n\n"

    import src.llm_core as llm_core_mod
    monkeypatch.setattr(llm_core_mod, "stream_llm", fake_slow_stream_llm)


# ---------------------------------------------------------------------------
# initial-synthesis stream_seat site
# ---------------------------------------------------------------------------

async def test_slow_synthesizer_times_out_and_run_still_reaches_done(monkeypatch):
    monkeypatch.setattr(orch, "SYNTH_TIMEOUT_S", 0.05)
    _make_stubs(monkeypatch)
    _patch_slow_stream_llm(monkeypatch, delay=1.0)

    events = await _run(_two_layer_cfg(), PROMPT)

    timeout_events = [
        e for e in events
        if e.get("type") == "seat_status" and e.get("status") == "error"
        and e.get("error") == "timeout"
    ]
    assert timeout_events, f"expected a timeout seat_status event, got: {events}"
    assert events[-1]["type"] == "done"


async def test_healthy_fast_synthesizer_is_unaffected(monkeypatch):
    monkeypatch.setattr(orch, "SYNTH_TIMEOUT_S", 180.0)
    _make_stubs(monkeypatch)

    events = await _run(_two_layer_cfg(), PROMPT)

    assert not any(
        e.get("type") == "seat_status" and e.get("error") == "timeout" for e in events
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["final"]


# ---------------------------------------------------------------------------
# verify-repair stream_seat site
# ---------------------------------------------------------------------------

async def test_repair_round_synthesizer_timeout_still_reaches_done(monkeypatch):
    """Force a verification failure (invalid Python) so the repair round
    runs, then make the REPAIR round's stream_seat call hang past
    SYNTH_TIMEOUT_S -- proves the SECOND (verify-repair) call site is also
    deadline-bounded, independently of the first."""
    monkeypatch.setattr(orch, "SYNTH_TIMEOUT_S", 0.05)
    _make_stubs(monkeypatch)

    calls = {"n": 0}

    async def fake_stream_llm(url, model, messages, temperature=1.0, headers=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # Initial synthesis: fast, but invalid code -> triggers repair.
            bad_code = "```python\ndef f(:\n```"
            yield f"data: {json.dumps({'delta': bad_code})}\n\n"
            yield "data: [DONE]\n\n"
        else:
            # Repair round: hangs past the deadline.
            await asyncio.sleep(1.0)
            yield f"data: {json.dumps({'delta': 'too late'})}\n\n"
            yield "data: [DONE]\n\n"

    import src.llm_core as llm_core_mod
    monkeypatch.setattr(llm_core_mod, "stream_llm", fake_stream_llm)

    events = await _run(_two_layer_cfg(), PROMPT, verify=True, verify_repair=True)

    timeout_events = [
        e for e in events
        if e.get("type") == "seat_status" and e.get("status") == "error"
        and e.get("error") == "timeout"
    ]
    assert timeout_events, f"expected a repair-round timeout event, got: {events}"
    assert events[-1]["type"] == "done"


# ---------------------------------------------------------------------------
# overall council-run wall-clock budget
# ---------------------------------------------------------------------------

async def test_over_budget_run_emits_run_level_error_and_still_finalizes_done(monkeypatch):
    """A 2-layer council whose first (batch/panelist) layer is slow enough
    that COUNCIL_RUN_TIMEOUT_S has already elapsed by the time the loop
    reaches layer 2 (the synthesizer) -- the run-level error event must fire
    at that layer boundary, the loop must break, and the existing fallback/
    verify/done code must still run so a done event is still emitted."""
    monkeypatch.setattr(orch, "COUNCIL_RUN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(orch, "SEAT_TIMEOUT_S", 5.0)
    _make_stubs(monkeypatch)

    import src.llm_core as llm_core_mod

    async def fake_llm_call_async(url, model, messages, temperature=1.0, headers=None, **kw):
        # The batch (panelist) seat in layer 1 -- slow enough that by the
        # time layer 2 (the synthesizer) is reached, the run deadline has
        # already passed.
        await asyncio.sleep(0.2)
        return "a slow but valid panelist answer"

    monkeypatch.setattr(llm_core_mod, "llm_call_async", fake_llm_call_async)

    events = await _run(_two_layer_cfg(), PROMPT)

    run_error_events = [
        e for e in events
        if e.get("type") == "error" and "wall-clock budget" in e.get("error", "")
    ]
    assert run_error_events, f"expected a run-level wall-clock error event, got: {events}"
    # The layer loop broke before the synthesizer (layer 2) ran, but the
    # existing fallback/verify/done code must still execute -- an honest
    # partial `done` event, never a silent drop or hang.
    assert events[-1]["type"] == "done"


async def test_under_budget_run_has_no_run_level_error_event(monkeypatch):
    monkeypatch.setattr(orch, "COUNCIL_RUN_TIMEOUT_S", 600.0)
    _make_stubs(monkeypatch)

    events = await _run(_two_layer_cfg(), PROMPT)

    assert not any(
        e.get("type") == "error" and "wall-clock budget" in e.get("error", "")
        for e in events
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["final"]
