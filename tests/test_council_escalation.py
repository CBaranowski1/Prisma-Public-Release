"""Hermetic tests for the council escalation primitive (RMEAS-02, Phase 40 Plan 01).

Proves (per 40-01-PLAN.md must_haves):
  - a resolvable per-request `escalate_model` swaps ONLY the existing repair
    round's synthesizer model (validate-before-swap via `normalize_model_id`)
    and emits exactly one `escalation_fired` event;
  - an unresolvable target does NOT swap, emits `escalation_failed` +
    a logged warning, and the repair round still completes normally
    (fail-safe to today's behavior);
  - `escalate_model=None` (and `""`) leaves the escalation code path
    completely inert -- `normalize_model_id` is never called and zero
    escalation events are emitted (the flag-off byte-for-byte parity
    contract, mirroring the fresh_context opt-out precedent);
  - escalation never adds a model call -- the repair round is still exactly
    ONE extra `stream_seat` call, whether or not escalation fires.

Offline / no-LLM: patches exactly three seams --
  (a) `orchestrator.stream_seat` -- a capturing async fake (records
      `seat.model` per call, yields scripted content whole);
  (b) `orchestrator.normalize_model_id` -- a scripted resolve fake;
  (c) `seat_runner._resolve` -- a fake returning a fixed (url, model, headers)
      tuple so the escalation branch can obtain the synth endpoint URL.
The real, offline `verified_gate`/`verify` gate is NEVER stubbed -- it is what
actually drives `needs_repair` from the scripted broken-then-valid Python.
"""
from __future__ import annotations

import asyncio
import logging

from core.constants import ESCALATION_RESOLVE_TIMEOUT_SECONDS
from src.council import orchestrator, seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat

PROMPT = "Write me some code."

# Fails ast.parse -> the real verified_gate flips needs_repair on the initial
# synthesis, forcing the single existing repair round to run.
BROKEN_PY = "Here you go:\n```python\ndef broken(:\n    pass\n```"
# Parses cleanly -> the repair round's real re-check passes.
VALID_PY = "Fixed:\n```python\ndef ok():\n    return 1\n```"


def _cfg() -> CouncilConfig:
    """A single synthesizer-only seat -- the smallest council that reaches
    the repair round (no panelists needed to force needs_repair)."""
    return CouncilConfig(seats=[
        Seat(id="chief", role="synthesizer", model="weak-model",
             endpoint_id="e1", consumes=[]),
    ])


def _stub_resolve(monkeypatch, url: str = "http://fake/chief", model: str = "weak-model"):
    monkeypatch.setattr(seat_runner, "_resolve",
                        lambda seat, owner: (url, model, {}))


def _stub_stream_seat(monkeypatch, contents: list[str]) -> list[str]:
    """Capturing async fake for `orchestrator.stream_seat`. Appends
    `seat.model` to `call_log` per call (in call order) and yields the next
    scripted content whole, as a single delta -- one item per call, the
    last item repeats if the queue runs out (defensive; every test here
    scripts exactly enough calls)."""
    call_log: list[str] = []
    queue = list(contents)

    async def fake_stream_seat(seat, prompt, upstream, owner=None, registry=None):
        call_log.append(seat.model)
        content = queue.pop(0) if queue else (contents[-1] if contents else "")
        yield content

    monkeypatch.setattr(orchestrator, "stream_seat", fake_stream_seat)
    return call_log


async def _run(cfg: CouncilConfig, prompt: str = PROMPT, **kw) -> list[dict]:
    """Drain run_council's async generator into a plain list of events,
    under a hang-ceiling so a stuck stub fails the test instead of the suite."""
    async def _drain():
        events = []
        async for ev in run_council(prompt, cfg, **kw):
            events.append(ev)
        return events
    return await asyncio.wait_for(_drain(), timeout=15)


async def test_escalation_swaps_repair_model_and_emits_escalation_fired(monkeypatch):
    cfg = _cfg()
    call_log = _stub_stream_seat(monkeypatch, [BROKEN_PY, VALID_PY])
    _stub_resolve(monkeypatch)

    normalize_calls: list[tuple] = []

    def fake_normalize(url, requested, timeout):
        normalize_calls.append((url, requested, timeout))
        return "strong-model-resolved"

    monkeypatch.setattr(orchestrator, "normalize_model_id", fake_normalize)

    events = await _run(cfg, escalate_model="strong-model")

    assert call_log == ["weak-model", "strong-model-resolved"], (
        "the repair round must run on the resolved escalation target -- "
        f"got {call_log}"
    )
    assert normalize_calls == [
        ("http://fake/chief", "strong-model", ESCALATION_RESOLVE_TIMEOUT_SECONDS)
    ]

    fired = [e for e in events if e["type"] == "escalation_fired"]
    assert len(fired) == 1, f"expected exactly one escalation_fired, got {fired}"
    assert fired[0]["seat_id"] == "chief"
    assert fired[0]["from"] == "weak-model"
    assert fired[0]["to"] == "strong-model-resolved"

    failed = [e for e in events if e["type"] == "escalation_failed"]
    assert not failed

    done = next(e for e in events if e["type"] == "done")
    assert done is not None


async def test_unresolvable_escalate_model_no_swap_no_fired_warns(monkeypatch, caplog):
    cfg = _cfg()
    call_log = _stub_stream_seat(monkeypatch, [BROKEN_PY, BROKEN_PY])
    _stub_resolve(monkeypatch)
    monkeypatch.setattr(orchestrator, "normalize_model_id",
                        lambda url, requested, timeout: None)

    with caplog.at_level(logging.WARNING, logger="src.council.orchestrator"):
        events = await _run(cfg, escalate_model="ghost-model")

    assert call_log == ["weak-model", "weak-model"], (
        "an unresolvable target must NOT swap -- the repair round stays on "
        f"the original model, got {call_log}"
    )

    fired = [e for e in events if e["type"] == "escalation_fired"]
    assert not fired

    failed = [e for e in events if e["type"] == "escalation_failed"]
    assert len(failed) == 1, f"expected exactly one escalation_failed, got {failed}"
    assert set(failed[0].keys()) == {"type", "seat_id", "target", "reason"}
    assert failed[0]["seat_id"] == "chief"
    assert failed[0]["target"] == "ghost-model"

    assert any("escalat" in rec.message.lower() for rec in caplog.records), (
        "an unresolvable escalation target must log a warning"
    )

    done = next(e for e in events if e["type"] == "done")
    assert done is not None


async def test_escalation_off_none_is_inert_normalize_never_called(monkeypatch):
    for escalate_model in (None, ""):
        call_log = _stub_stream_seat(monkeypatch, [BROKEN_PY, VALID_PY])
        _stub_resolve(monkeypatch)

        def sentinel(*a, **kw):
            raise AssertionError(
                "normalize_model_id must never be called when escalate_model "
                "is falsy"
            )

        monkeypatch.setattr(orchestrator, "normalize_model_id", sentinel)

        events = await _run(_cfg(), escalate_model=escalate_model)

        assert call_log == ["weak-model", "weak-model"], (
            f"escalate_model={escalate_model!r} must be fully inert, got {call_log}"
        )
        assert not any("escalat" in e["type"] for e in events), (
            f"escalate_model={escalate_model!r} must emit zero escalation "
            f"events, got {[e['type'] for e in events]}"
        )


async def test_escalation_adds_no_extra_model_call(monkeypatch):
    cases = [
        ("strong-model", "strong-model-resolved"),  # resolvable
        (None, None),                                # off
    ]
    for escalate_model, normalize_ret in cases:
        call_log = _stub_stream_seat(monkeypatch, [BROKEN_PY, VALID_PY])
        _stub_resolve(monkeypatch)
        monkeypatch.setattr(
            orchestrator, "normalize_model_id",
            lambda url, requested, timeout, _ret=normalize_ret: _ret,
        )

        await _run(_cfg(), escalate_model=escalate_model)

        assert len(call_log) == 2, (
            f"escalate_model={escalate_model!r}: expected exactly one initial "
            f"+ one repair call (2 total), got {len(call_log)}: {call_log}"
        )


async def test_no_escalation_events_when_escalate_model_absent(monkeypatch):
    """Regression lock (Task 2): the standard (no-escalate) repair path is
    untouched -- the normal verify/repair/done event sequence fires with zero
    escalation vocabulary when `escalate_model` isn't even passed as a kwarg."""
    cfg = _cfg()
    _stub_stream_seat(monkeypatch, [BROKEN_PY, VALID_PY])
    _stub_resolve(monkeypatch)

    events = await _run(cfg)  # no escalate_model kwarg at all

    types = [e["type"] for e in events]
    assert not any("escalat" in t for t in types), (
        f"zero escalation vocabulary expected, got {types}"
    )

    verification_events = [e for e in events if e["type"] == "verification"]
    stages = {e["stage"] for e in verification_events}
    assert stages == {"initial", "repair"}, (
        "the standard repair round must still fire without escalate_model"
    )

    assert events[-1]["type"] == "done"
    assert events[-1]["verified"] is True
