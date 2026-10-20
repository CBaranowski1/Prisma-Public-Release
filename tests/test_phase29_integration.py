"""Phase 29 integration-checker connection (SC-5).

Proves the phase's headline resilience behaviors on the paths the app
actually assembles — not only on the literally-named helpers — converting
GRAY-AGT-04/06/07 from "presence" to "integration":

  - an unresolvable escalation target is CAUGHT on the assembled
    `stream_agent_loop` path: a `model_escalation_failed` event fires and NO
    swap into the dead model ever happens (GRAY-AGT-04, 29-01);
  - a cyclic `CouncilConfig` makes `run_council` — driven DIRECTLY, not via
    `routes/council_routes.py`'s blanket try/except — yield an
    `{"type": "error", ...}` event instead of an exception escaping the
    generator (GRAY-AGT-07, 29-02);
  - both drives are bounded by an outer `asyncio.wait_for` ceiling so a
    hang/timeout regression fails LOUD in CI instead of wedging the suite
    (GRAY-AGT-06 safety net).

The live on-machine confirmation (a real server SSE turn: an unresolvable
escalation model on a real endpoint, a cyclic/malformed council config over a
real SSE stream, and a wedged synthesizer degrading to an honest partial
`done`) is proven by the phase's human on-machine smoke (this plan's Task 2),
not by this hermetic suite.

Hermetic: no real LLM, network, DB, or Docker daemon. The Chat-half drive
reuses the phase's own shipped offline `stream_agent_loop` harness seams from
`tests.test_agent_loop_verify_gate` (plain helper functions, not pytest
fixtures) — the same seams `tests/test_escalation_resolve.py` and
`tests/test_phase28_integration.py` already reuse. The Council-half drive
calls `run_council` directly with the offline model/DB-resolver stubs from
`tests.test_council_fresh_context_integration` (`_make_stubs`), the same
seams `tests/test_council_dag_error_event.py` already reuses. No bespoke
cross-module pytest fixture is imported; only plain helper functions.
"""
import asyncio

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
from tests.test_council_fresh_context_integration import PROMPT, _make_stubs


# ---------------------------------------------------------------------------
# Chat half (GRAY-AGT-04) — inline offline driver for the assembled
# stream_agent_loop escalation path (mirrors test_phase28_integration.py's
# _drive_agent_loop, kept local so this checker owns its drive; additionally
# captures the MODEL used on every round, mirroring
# tests/test_escalation_resolve.py's model_log capture — no existing SSE
# event otherwise exposes whether _escalate() actually swapped the model).
# ---------------------------------------------------------------------------
def _make_fake_normalize(resolved_id, call_log=None):
    """Fake `normalize_model_id(endpoint_url, requested, timeout=...)`.

    Returns `resolved_id` (None here, to script an unresolvable target) and
    records every call in `call_log` if given.
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
        session_id="sess-sc5-esc",
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


async def test_unresolvable_escalation_is_caught_no_swap_sc5(monkeypatch):
    # Round 1 fires a tool call (keeps the loop going, triggers the
    # round-budget escalation check before round 2's LLM call); round 2 is
    # the plain-prose final answer — mirrors
    # tests/test_escalation_resolve.py::test_agent_escalate_unresolvable_stays_and_emits_failed,
    # but driven here as this phase's own integration checker.
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
    events = await _drive_agent_loop_escalation(
        monkeypatch, rounds,
        settings={"agent_escalation_model": "unresolvable-dead-model"},
        model_log=model_log,
        max_rounds=2,
    )

    # The escalation-resolve seam was actually exercised...
    assert len(call_log) == 1
    assert call_log[0][:2] == ("http://localhost:11434", "unresolvable-dead-model")

    # ...the model used on EVERY round is unchanged — no swap into the dead
    # model ever happens.
    assert model_log == ["test-model", "test-model"]

    # ...and the assembled stream surfaces the bounded failure event (never a
    # model_escalation swap event).
    failed = _typed_events(events, "model_escalation_failed")
    assert len(failed) == 1
    assert failed[0]["target"] == "unresolvable-dead-model"
    assert "reason" in failed[0]
    assert set(failed[0].keys()) == {"type", "target", "reason"}
    assert not _typed_events(events, "model_escalation")

    # The turn still completed normally past the failed escalation attempt.
    assert "final answer" in "".join(events)


# ---------------------------------------------------------------------------
# Council half (GRAY-AGT-07) — drive run_council DIRECTLY (not via
# routes/council_routes.py) with a cyclic CouncilConfig, using the offline
# model/DB-resolver stubs from tests/test_council_fresh_context_integration.py
# (the same seams tests/test_council_dag_error_event.py already reuses).
# ---------------------------------------------------------------------------
def _cyclic_cfg() -> CouncilConfig:
    """Two seats that consume each other — a direct 2-cycle."""
    return CouncilConfig(seats=[
        Seat(id="a", role="panelist", consumes=["b"]),
        Seat(id="b", role="synthesizer", consumes=["a"]),
    ])


async def _drive_council_cyclic(monkeypatch):
    _make_stubs(monkeypatch)

    async def _consume():
        out = []
        async for ev in run_council(PROMPT, _cyclic_cfg()):
            out.append(ev)
        return out

    # Outer ceiling: a hang/timeout regression must fail LOUD, never wedge CI.
    return await asyncio.wait_for(_consume(), timeout=15)


async def test_cyclic_dag_yields_error_event_no_exception_sc5(monkeypatch):
    events = await _drive_council_cyclic(monkeypatch)

    assert events, "run_council must yield at least one event"
    assert events[0]["type"] == "error"
    err = events[0]["error"].lower()
    assert "cycle" in err or "missing dependency" in err
    # Generator ends cleanly right after the error event — no exception
    # escaped the async generator, and no run_started followed it.
    assert len(events) == 1
