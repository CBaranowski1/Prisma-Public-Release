"""Hermetic proof that the council/escalation arm genuinely fires the
Phase-40 escalation primitive through the REAL (unstubbed) Ruby
EXECUTION-tier verify gate (Plan 01, D-09) -- R-1's mandatory pre-live-run
gate (D-05: no live preflight, so this offline proof is the safety net).

Mirrors tests/test_council_escalation.py's stub-seam convention EXACTLY,
with one deliberate difference: the scripted synthesizer content is RUBY
(not Python), so the REAL, unstubbed src/council/verify.py Ruby EXECUTION
tier (added in Plan 01) is what actually drives `needs_repair` -- never a
stubbed/faked verify result.

Offline / no-LLM: patches exactly three seams --
  (a) `orchestrator.stream_seat` -- a capturing async fake (records
      `seat.model` per call, yields scripted content whole);
  (b) `orchestrator.normalize_model_id` -- a scripted resolve fake;
  (c) `seat_runner._resolve` -- a fake returning a fixed (url, model, headers)
      tuple so the escalation branch can obtain the synth endpoint URL.
The real, offline `src/council/verify.py` Ruby execution tier is NEVER
stubbed -- it is what actually flips `needs_repair` from the scripted
broken-then-valid Ruby (proving council_arm.py's escalation arm, which
drives this exact `run_council` call shape via `H.run_real_arm`, will
genuinely fire on the live n>=60 run).

Drives `run_council` directly (as tests/test_council_escalation.py does) --
the most direct exercise of the wiring under test, with zero live-model
dependence. Guarded by a `shutil.which("ruby")` skip so a ruby-less CI still
passes collection; each drain runs under a hang-ceiling
(`asyncio.wait_for`) so a stuck stub fails the test instead of the suite.
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

from src.council import orchestrator, seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat

pytestmark = pytest.mark.skipif(
    shutil.which("ruby") is None,
    reason="ruby not on PATH -- the REAL Ruby execution-tier verify gate "
           "this test proves against needs a live ruby interpreter",
)

WEAK_MODEL = "weak-model"
STRONG_MODEL_REQUESTED = "strong-model"
STRONG_MODEL_RESOLVED = "strong-model-resolved"

# Fails to parse -- the REAL verify.py Ruby execution tier (Plan 01, D-09)
# flips needs_repair on the initial synthesis, forcing the single existing
# repair round to run. Mirrors tests/test_verify_ruby_exec.py's BROKEN_RUBY.
BROKEN_RUBY = "Here:\n```ruby\ndef foo(\n  bar\n```"
# Parses AND loads clean -- the repair round's real re-check passes.
VALID_RUBY = "Fixed:\n```ruby\ndef ok\n  1\nend\n```"
# Syntactically valid, loads without crashing, but its logic is wrong
# (subtracts instead of adds) -- no hidden tests in a general council prompt
# (verify.py's D-02 contract), so this must stay passed=True, needs_repair
# False. Mirrors tests/test_verify_ruby_exec.py's WRONG_LOGIC_RUBY.
WRONG_LOGIC_RUBY = "Here:\n```ruby\ndef add(a, b)\n  a - b\nend\n```"


def _cfg() -> CouncilConfig:
    """A single synthesizer-only seat -- the smallest council that reaches
    the repair round (no panelists needed to force needs_repair); mirrors
    tests/test_council_escalation.py's `_cfg`."""
    return CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", model=WEAK_MODEL,
             endpoint_id="e1", consumes=[]),
    ])


def _stub_resolve(monkeypatch, url: str = "http://fake/chief", model: str = WEAK_MODEL):
    monkeypatch.setattr(seat_runner, "_resolve", lambda seat, owner: (url, model, {}))


def _stub_stream_seat(monkeypatch, contents: list[str]) -> list[str]:
    """Capturing async fake for `orchestrator.stream_seat`. Appends
    `seat.model` to `call_log` per call (in call order) and yields the next
    scripted content whole, as a single delta."""
    call_log: list[str] = []
    queue = list(contents)

    async def fake_stream_seat(seat, prompt, upstream, owner=None, registry=None):
        call_log.append(seat.model)
        content = queue.pop(0) if queue else (contents[-1] if contents else "")
        yield content

    monkeypatch.setattr(orchestrator, "stream_seat", fake_stream_seat)
    return call_log


async def _run(cfg: CouncilConfig, prompt: str = "Write me some ruby code.", **kw) -> list[dict]:
    """Drain run_council's async generator into a plain list of events,
    under a hang-ceiling so a stuck stub fails the test instead of the
    suite."""
    async def _drain():
        events = []
        async for ev in run_council(prompt, cfg, **kw):
            events.append(ev)
        return events
    return await asyncio.wait_for(_drain(), timeout=15)


async def test_escalation_fires_genuinely_through_real_ruby_exec_verify_gate(monkeypatch):
    """(1) broken Ruby flips needs_repair via the REAL Ruby execution tier
    -> the single existing repair round runs (two stream_seat calls).
    (2) the repair round's stream_seat call is made with the RESOLVED
    strong model. (3) exactly one escalation_fired event, from=weak,
    to=strong."""
    cfg = _cfg()
    call_log = _stub_stream_seat(monkeypatch, [BROKEN_RUBY, VALID_RUBY])
    _stub_resolve(monkeypatch)

    normalize_calls: list[tuple] = []

    def fake_normalize(url, requested, timeout):
        normalize_calls.append((url, requested, timeout))
        return STRONG_MODEL_RESOLVED

    monkeypatch.setattr(orchestrator, "normalize_model_id", fake_normalize)

    events = await _run(cfg, verify_execute=True, escalate_model=STRONG_MODEL_REQUESTED)

    # (1) the repair round genuinely ran -- driven by the REAL, unstubbed
    # verify.py Ruby execution tier flipping needs_repair on BROKEN_RUBY.
    assert len(call_log) == 2, (
        f"expected initial synthesis + exactly one repair round, got {call_log}"
    )
    # (2) the repair round used the STRONG (resolved) model, not the weak one.
    assert call_log == [WEAK_MODEL, STRONG_MODEL_RESOLVED], (
        f"the repair round must run on the resolved escalation target, got {call_log}"
    )
    assert normalize_calls, "normalize_model_id must have been called to resolve the swap"

    # (3) exactly one escalation_fired event.
    fired = [e for e in events if e["type"] == "escalation_fired"]
    assert len(fired) == 1, f"expected exactly one escalation_fired, got {fired}"
    assert fired[0]["from"] == WEAK_MODEL
    assert fired[0]["to"] == STRONG_MODEL_RESOLVED

    failed = [e for e in events if e["type"] == "escalation_failed"]
    assert not failed

    # The REAL verify gate's initial verdict must show the genuine failure
    # this whole chain depends on -- not a stubbed/faked one.
    initial_verification = next(
        e for e in events if e["type"] == "verification" and e["stage"] == "initial")
    assert initial_verification["passed"] is False, (
        f"BROKEN_RUBY must genuinely fail the real Ruby execution tier, "
        f"got {initial_verification}"
    )

    done = next(e for e in events if e["type"] == "done")
    assert done["verified"] is True, (
        f"the repaired, valid Ruby must be genuinely re-verified, got {done}"
    )


async def test_well_formed_but_wrong_ruby_does_not_flip_needs_repair(monkeypatch):
    """(4) A syntactically-valid Ruby def whose logic is wrong but which
    loads without crashing must NOT reach the repair round -- the escalation
    code path is never entered: zero escalation_fired/failed events, no
    second stream_seat call, no model swap. Confirms the syntax/logic
    boundary from Plan 01's D-02 contract holds under escalation too."""
    cfg = _cfg()
    call_log = _stub_stream_seat(monkeypatch, [WRONG_LOGIC_RUBY])
    _stub_resolve(monkeypatch)

    def sentinel(*a, **kw):
        raise AssertionError(
            "normalize_model_id must never be called -- the escalation path "
            "must not be reached when needs_repair stays False"
        )

    monkeypatch.setattr(orchestrator, "normalize_model_id", sentinel)

    events = await _run(cfg, verify_execute=True, escalate_model=STRONG_MODEL_REQUESTED)

    assert call_log == [WEAK_MODEL], (
        f"a syntactically-valid-but-logically-wrong Ruby answer must NOT "
        f"trigger a repair round, got {call_log}"
    )

    assert not [e for e in events if e["type"] == "escalation_fired"]
    assert not [e for e in events if e["type"] == "escalation_failed"]

    initial_verification = next(
        e for e in events if e["type"] == "verification" and e["stage"] == "initial")
    assert initial_verification["passed"] is True, (
        f"logically-wrong-but-loading Ruby must stay non-blocking (no hidden "
        f"tests, verify.py's D-02 contract), got {initial_verification}"
    )

    verification_stages = {e["stage"] for e in events if e["type"] == "verification"}
    assert verification_stages == {"initial"}, (
        f"no repair-stage verification event expected, got {verification_stages}"
    )

    done = next(e for e in events if e["type"] == "done")
    assert done["verified"] is True
