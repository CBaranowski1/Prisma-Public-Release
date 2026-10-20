"""tests/test_escalation_resolve.py — GRAY-AGT-04 escalation-target resolve
check (29-01-PLAN.md) + RMEAS-02 council escalation primitive proof
(40-03-PLAN.md).

Offline, hermetic — no live LLM/DB/network. Reuses the offline agent-loop
harness from tests/test_agent_loop_verify_gate.py (fake stream_llm_with_fallback
/ fake get_setting) to drive `src.agent_loop.stream_agent_loop`, and mirrors
tests/test_untrusted_context_policy_coverage.py's `httpx.post` monkeypatch
shape for the Answer path. `normalize_model_id` is monkeypatched at the module
boundary (`agent_loop.normalize_model_id` / `answer_pipeline.normalize_model_id`
/ `orchestrator.normalize_model_id`) to script resolve/no-resolve outcomes so
no live endpoint is ever touched.

Behavior pinned here (see 29-01-PLAN.md <behavior> + 40-03-PLAN.md):
  - test_agent_escalate_resolves_swaps_model
  - test_agent_escalate_unresolvable_stays_and_emits_failed
  - test_agent_escalate_ladder_spends_on_failure
  - test_answer_author_fn_unresolvable_falls_back_to_model
  - test_answer_author_fn_no_escalate_is_byte_identical
  - test_council_escalation_primitive_now_exists
  - test_council_no_escalate_is_byte_identical
"""
from __future__ import annotations

import asyncio
import inspect
import json

import httpx

import src.answer_pipeline as answer_pipeline
from src.council import orchestrator
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat
from tests.test_council_fresh_context_integration import _make_stubs
from tests.test_agent_loop_verify_gate import (
    agent_loop,
    settings_module,
    tool_index_module,
    _make_fake_get_setting,
    _make_fake_stream,
    _delta_chunks,
    _tool_call_chunks,
    _typed_events,
)


# ---------------------------------------------------------------------------
# Agent path (`_escalate`) harness — mirrors run_chat() in
# tests/test_agent_loop_verify_gate.py, additionally capturing the MODEL used
# on every round (candidates[0][1] of the (endpoint_url, model, headers)
# tuple stream_agent_loop passes to stream_llm_with_fallback) — needed to
# prove _escalate() actually swaps (or does not swap) the active model, which
# no existing SSE event exposes.
# ---------------------------------------------------------------------------

async def _drive_agent_loop(monkeypatch, rounds, *, settings=None, model_log=None, **loop_kwargs):
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
        messages=[{"role": "user", "content": "hello"}],
        owner="owner-1",
        session_id=None,
        max_rounds=len(rounds),
    )
    default_kwargs.update(loop_kwargs)

    events = []
    async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
        events.append(chunk)
    return events


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


class TestAgentEscalateResolveCheck:
    async def test_agent_escalate_resolves_swaps_model(self, monkeypatch):
        # Round 1 fires a tool call (keeps the loop going); the round-budget
        # escalation trigger fires before round 2's LLM call
        # (round_num=2 > 1 and round_num*2=4 >= max_rounds=2); round 2 is the
        # plain-prose final answer.
        rounds = [
            _tool_call_chunks("read_file", {"path": "foo.txt"}),
            _delta_chunks("final answer"),
        ]
        call_log = []
        monkeypatch.setattr(
            agent_loop, "normalize_model_id",
            _make_fake_normalize("big-model-resolved", call_log),
            raising=False,
        )

        model_log = []
        events = await _drive_agent_loop(
            monkeypatch, rounds,
            settings={"agent_escalation_model": "big-model"},
            model_log=model_log,
            max_rounds=2,
        )

        assert model_log == ["test-model", "big-model-resolved"]
        assert len(call_log) == 1
        assert call_log[0][:2] == ("http://localhost:11434", "big-model")

        esc_events = _typed_events(events, "model_escalation")
        assert len(esc_events) == 1
        assert esc_events[0]["from"] == "test-model"
        assert esc_events[0]["to"] == "big-model-resolved"
        assert not _typed_events(events, "model_escalation_failed")

    async def test_agent_escalate_unresolvable_stays_and_emits_failed(self, monkeypatch):
        rounds = [
            _tool_call_chunks("read_file", {"path": "foo.txt"}),
            _delta_chunks("final answer"),
        ]
        call_log = []
        monkeypatch.setattr(
            agent_loop, "normalize_model_id",
            _make_fake_normalize(None, call_log),
            raising=False,
        )

        model_log = []
        events = await _drive_agent_loop(
            monkeypatch, rounds,
            settings={"agent_escalation_model": "big-model"},
            model_log=model_log,
            max_rounds=2,
        )

        # Model is UNCHANGED across both rounds — never swapped to the
        # unresolvable target.
        assert model_log == ["test-model", "test-model"]
        assert len(call_log) == 1

        failed_events = _typed_events(events, "model_escalation_failed")
        assert len(failed_events) == 1
        assert failed_events[0]["target"] == "big-model"
        assert "reason" in failed_events[0]
        # T-29-esc: bounded surface only — no raw HTTP/exception detail.
        assert set(failed_events[0].keys()) == {"type", "target", "reason"}
        assert not _typed_events(events, "model_escalation")

    async def test_agent_escalate_ladder_spends_on_failure(self, monkeypatch):
        # 3 rounds: round 1 + round 2 each fire a tool call (keeps the loop
        # alive past the round-budget trigger at round 2 AND round 3); round
        # 3 is the plain-prose final answer. The trigger condition
        # (round_num > 1 and round_num*2 >= max_rounds) is true at BOTH
        # round 2 (2*2=4>=3) and round 3 (3*2=6>=3) — proving the ladder only
        # spends ONCE (at round 2, on the failed resolve) and does not
        # retry-storm at round 3.
        rounds = [
            _tool_call_chunks("read_file", {"path": "foo.txt"}),
            _tool_call_chunks("read_file", {"path": "bar.txt"}),
            _delta_chunks("final answer"),
        ]
        call_log = []
        monkeypatch.setattr(
            agent_loop, "normalize_model_id",
            _make_fake_normalize(None, call_log),
            raising=False,
        )

        model_log = []
        events = await _drive_agent_loop(
            monkeypatch, rounds,
            settings={"agent_escalation_model": "big-model"},
            model_log=model_log,
            max_rounds=3,
        )

        assert model_log == ["test-model", "test-model", "test-model"]
        # Exactly ONE resolve attempt across the whole turn — the ladder
        # spent itself on the first (failed) attempt and never retried.
        assert len(call_log) == 1
        assert len(_typed_events(events, "model_escalation_failed")) == 1


# ---------------------------------------------------------------------------
# Answer path (`wire_defaults().author_fn`)
# ---------------------------------------------------------------------------

class _FakeHttpResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class TestAnswerAuthorFnResolveCheck:
    def test_answer_author_fn_unresolvable_falls_back_to_model(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["model"] = json["model"]
            return _FakeHttpResponse("a thorough answer")

        monkeypatch.setattr(httpx, "post", fake_post)
        monkeypatch.setattr(
            answer_pipeline, "normalize_model_id",
            _make_fake_normalize(None), raising=False,
        )

        fns = answer_pipeline.wire_defaults(
            "test-model", registry=None, escalate_model="big-model",
        )
        out = fns["author_fn"]("some draft prompt")

        assert out == "a thorough answer"
        # Falls back to the WORKING model — never dispatches the unresolved
        # escalate_model.
        assert captured["model"] == "test-model"

    def test_answer_author_fn_no_escalate_is_byte_identical(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["model"] = json["model"]
            captured["messages"] = json["messages"]
            return _FakeHttpResponse("a thorough answer")

        monkeypatch.setattr(httpx, "post", fake_post)

        def _raise_if_called(*args, **kwargs):
            raise AssertionError(
                "normalize_model_id must NEVER be called when escalate_model "
                "is falsy — byte-identical no-escalation contract violated"
            )

        monkeypatch.setattr(
            answer_pipeline, "normalize_model_id", _raise_if_called, raising=False,
        )

        # No escalate_model at all — the exact non-regression precondition
        # tests/test_untrusted_context_policy_coverage.py:151-187 relies on.
        fns = answer_pipeline.wire_defaults("test-model", registry=None)
        out = fns["author_fn"]("some draft prompt")

        assert out == "a thorough answer"
        assert captured["model"] == "test-model"


# ---------------------------------------------------------------------------
# Council — RMEAS-02 (Phase 40): the council escalation primitive is now
# BUILT at the orchestrator level (run_council's `escalate_model` param,
# src/council/orchestrator.py) as a per-request, opt-in weak->strong
# synthesizer-model swap riding the EXISTING verify-gate repair round. The
# `Seat` schema is deliberately UNCHANGED — escalation is an orchestrator-
# side `model_copy` override, not a Seat field (see
# tests/test_deferred_smoke_p29_escalation_council.py's
# test_council_seat_has_no_escalation_or_swap_field for that still-correct
# design invariant). The full swap / unresolvable-fail-safe / no-extra-call
# behavioral proofs live in tests/test_council_escalation.py; this file only
# pins (1) presence and (2) flag-off byte-for-byte parity, mirroring its
# agent/answer siblings above.
# ---------------------------------------------------------------------------

def test_council_escalation_primitive_now_exists():
    """Positive presence proof — CANNOT pass green-by-accident. Fails loudly
    if the escalation primitive is ever reverted. See
    tests/test_council_escalation.py for the full swap/parity behavior."""
    sig = inspect.signature(run_council)
    assert "escalate_model" in sig.parameters, (
        "run_council must accept an escalate_model parameter — the RMEAS-02 "
        "council escalation primitive is missing"
    )
    assert sig.parameters["escalate_model"].default is None


_COUNCIL_PROMPT = "What should we do?"

# Fails ast.parse -> the real verified_gate flips needs_repair on the initial
# synthesis, forcing the single existing repair round to run (mirrors
# tests/test_council_escalation.py's BROKEN_PY/VALID_PY fixtures).
_BROKEN_PY = "Here you go:\n```python\ndef broken(:\n    pass\n```"
_VALID_PY = "Fixed:\n```python\ndef ok():\n    return 1\n```"


def _council_two_layer_cfg() -> CouncilConfig:
    """One panelist consumed by one synthesizer — minimal standard council."""
    return CouncilConfig(seats=[
        Seat(id="p1", role="panelist", consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["p1"]),
    ])


async def test_council_no_escalate_is_byte_identical(monkeypatch):
    """Council counterpart of test_answer_author_fn_no_escalate_is_byte_identical
    above: escalate_model=None (and the field omitted entirely) must leave
    normalize_model_id never-called and emit zero escalation events, even
    when the real verify gate fires the existing repair round."""

    def _raise_if_called(*args, **kwargs):
        raise AssertionError(
            "normalize_model_id must NEVER be called when escalate_model "
            "is falsy — byte-identical no-escalation contract violated"
        )

    for kwargs in ({"escalate_model": None}, {}):
        _make_stubs(
            monkeypatch,
            scripted={"p1": ["p1 answer, short and simple."],
                      "chief": [_BROKEN_PY, _VALID_PY]},
        )
        monkeypatch.setattr(orchestrator, "normalize_model_id", _raise_if_called)

        async def _drain():
            out = []
            async for ev in run_council(_COUNCIL_PROMPT, _council_two_layer_cfg(), **kwargs):
                out.append(ev)
            return out

        events = await asyncio.wait_for(_drain(), timeout=15)

        assert events[-1]["type"] == "done"
        assert not any("escalat" in e.get("type", "") for e in events), (
            f"escalate_model kwargs={kwargs!r} must emit zero escalation "
            f"events, got {[e['type'] for e in events]}"
        )
