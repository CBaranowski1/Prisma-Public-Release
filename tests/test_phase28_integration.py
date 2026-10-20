"""Phase 28 integration-checker connection (SC-5).

Proves the phase's headline agent-loop-robustness behaviors on the paths the app
actually assembles — not only on the literally-named helpers — converting
GRAY-AGT-01/02/03/05 from "presence" to "integration":

  - a runaway tool is TIMED OUT on the assembled stream_agent_loop path without
    hanging the SSE turn (GRAY-AGT-01);
  - over-limit bash/python output is fed back TRUNCATED but the full text is
    RETRIEVABLE via the ownership-scoped store, and a mismatched owner is
    REJECTED (GRAY-AGT-02);
  - a mid-stream LLM drop AFTER content started retries with bounded backoff and
    RECOVERS on the assembled stream_llm_with_fallback path (GRAY-AGT-05);
  - (assembled bonus) a healthy long turn emits round_ceiling_continuation
    (GRAY-AGT-03; unit-proven in plan 05).

The live on-machine confirmation (real server SSE turn: a genuinely hanging
tool, a real over-limit dump retrieved by id, a real mid-stream host drop, and
Council-unchanged) is proven by the phase's human on-machine smoke (this plan's
Task 2), not by this hermetic suite.

Hermetic: no real LLM, network, DB, or Docker daemon. The agent-loop drives
reuse the phase's own shipped offline stream_agent_loop harness seams (per this
plan's action — "import only the public run_chat/harness the phase already
ships"); the store + mid-stream drives use the REAL production modules
(src.tool_execution, src.tool_output_store, src.llm_core) directly. No bespoke
cross-module pytest fixture is imported.
"""
import asyncio

from core.constants import (
    MAX_OUTPUT_CHARS,
    CONTINUATION_ROUND_BATCH,
    MID_STREAM_RETRY_MAX_ATTEMPTS,
)

# Phase-shipped offline agent-loop harness seams (the import-stub dance that
# makes src.agent_loop importable without a live DB lives in this module; every
# Phase 28 agent-loop test reuses it — these are plain helper functions, not
# pytest fixtures).
from tests.test_agent_loop_verify_gate import (
    agent_loop,
    settings_module,
    tool_index_module,
    _make_fake_get_setting,
    _make_fake_stream,
    _delta_chunks,
    _typed_events,
    _extract_deltas,
)


# ---------------------------------------------------------------------------
# Inline offline driver for the assembled stream_agent_loop path (mirrors the
# run_chat seams; kept local so this checker owns its drive).
# ---------------------------------------------------------------------------
async def _drive_agent_loop(monkeypatch, rounds, *, tool_override=None, settings=None, **loop_kwargs):
    fake_get_setting = _make_fake_get_setting(settings)
    fake_stream = _make_fake_stream(rounds)

    async def _default_tool(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        return "tool executed (stub)", {"exit_code": 0, "output": ""}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", tool_override or _default_tool)
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
        session_id="sess-sc5",
        max_rounds=len(rounds) + 2,
    )
    default_kwargs.update(loop_kwargs)

    async def _consume():
        out = []
        async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
            out.append(chunk)
        return out

    # Outer ceiling: a timeout regression must fail LOUD, never hang CI.
    return await asyncio.wait_for(_consume(), timeout=15)


# ---------------------------------------------------------------------------
# GRAY-AGT-01 — a runaway tool is timed out on the assembled loop path.
# ---------------------------------------------------------------------------
async def test_runaway_tool_is_timed_out_sc5(monkeypatch):
    async def _slow_tool(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        await asyncio.sleep(5.0)  # far past the tiny wall-clock below
        return "SHOULD NOT REACH", {"output": "SHOULD NOT REACH", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "TOOL_WALL_CLOCK_TIMEOUT_SECONDS", 0.1)
    rounds = [
        _delta_chunks("```bash\nsleep 999\n```"),        # round 1: the hanging tool
        _delta_chunks("Done — the tool was bounded."),   # round 2: normal answer
    ]
    events = await _drive_agent_loop(monkeypatch, rounds, tool_override=_slow_tool)

    # The assembled turn completed (round 2 streamed) — it did NOT hang.
    assert "the tool was bounded" in "".join(_extract_deltas(events))
    # The bash tool surfaced a timeout result (exit_code 124), and the
    # would-be success output never reached the stream.
    bash_out = [e for e in _typed_events(events, "tool_output") if e.get("tool") == "bash"]
    assert bash_out and bash_out[0].get("exit_code") == 124, f"expected timeout 124: {bash_out}"
    assert "SHOULD NOT REACH" not in "".join(events)


# ---------------------------------------------------------------------------
# GRAY-AGT-02 — truncated bash/python output is retrievable in full, scoped.
# ---------------------------------------------------------------------------
def test_truncated_tool_output_is_retrievable_in_full_sc5():
    # Use the REAL truncation-with-indicator + store handoff (production path).
    import re
    from src import tool_output_store
    from src.tool_execution import _truncate_with_indicator

    session_id, owner = "sess-sc5-store", "owner-sc5"
    full = "L" + ("x" * (MAX_OUTPUT_CHARS + 5000)) + "END"
    indicated = _truncate_with_indicator(full, MAX_OUTPUT_CHARS, session_id=session_id, owner=owner)

    # Model-visible text is truncated AND carries a resolvable id, not a silent cut.
    assert len(indicated) < len(full)
    m = re.search(r"full output: ([0-9a-f]+)\]", indicated)
    assert m, f"no retrieval id in indicator: {indicated[-120:]!r}"
    output_id = m.group(1)

    # Owner can retrieve the FULL untruncated text...
    got = tool_output_store.get(output_id, session_id=session_id, owner=owner)
    assert got == full, "owner retrieval must return the full untruncated text"
    # ...a mismatched owner is REJECTED (Information-Disclosure access-scoping)...
    assert tool_output_store.get(output_id, session_id=session_id, owner="someone-else") is None
    # ...and a mismatched session is REJECTED too.
    assert tool_output_store.get(output_id, session_id="other-sess", owner=owner) is None


# ---------------------------------------------------------------------------
# GRAY-AGT-05 — a mid-stream drop after content started retries and recovers on
# the assembled stream_llm_with_fallback path.
# ---------------------------------------------------------------------------
def _drive_fallback(monkeypatch, attempts_lines):
    import src.llm_core as llm_core

    state = {"n": 0}

    async def fake_stream(url, model, messages, **kw):
        idx = min(state["n"], len(attempts_lines) - 1)
        state["n"] += 1
        for ln in attempts_lines[idx]:
            yield ln

    backoffs = []

    async def fake_sleep(secs):
        backoffs.append(secs)

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    monkeypatch.setattr(llm_core.asyncio, "sleep", fake_sleep)

    async def run():
        out = []
        async for c in llm_core.stream_llm_with_fallback(
            [("u1", "primary", {})], [{"role": "user", "content": "hi"}]
        ):
            out.append(c)
        return out

    return asyncio.run(run()), state["n"], backoffs


def test_mid_stream_drop_retries_sc5(monkeypatch):
    # Attempt 1: real content then a mid-stream error. Attempt 2: clean recovery.
    attempts = [
        ['data: {"delta": "partial"}\n\n', 'event: error\ndata: {"status": 503, "text": "reset"}\n\n'],
        ['data: {"delta": "recovered-content"}\n\n', "data: [DONE]\n\n"],
    ]
    chunks, calls, backoffs = _drive_fallback(monkeypatch, attempts)
    joined = "".join(chunks)
    # Recovered rather than ending on a bare terminal error...
    assert "recovered-content" in joined, f"no recovery: {chunks}"
    assert not chunks[-1].startswith("event: error"), f"ended on error: {chunks}"
    # ...and the retry is BOUNDED (no storm): one retry, one backoff here, and
    # the mechanism is capped at MID_STREAM_RETRY_MAX_ATTEMPTS overall.
    assert calls == 2, f"expected exactly one retry (2 calls), got {calls}"
    assert len(backoffs) == 1
    assert MID_STREAM_RETRY_MAX_ATTEMPTS >= 1


# ---------------------------------------------------------------------------
# GRAY-AGT-03 (assembled bonus) — continuation surfaces on the assembled path.
# ---------------------------------------------------------------------------
async def test_round_ceiling_emits_continuation_sc5(monkeypatch):
    # A distinct-tool-call every round (never trips the loop-breaker), a tiny
    # ceiling, opt-in True → the assembled loop must emit round_ceiling_continuation.
    state = {"n": 0}

    async def _distinct_stream(*_a, **_k):
        i = state["n"]
        state["n"] += 1
        text = f"```bash\necho step-{i}\n```" if i < 5 else "All steps complete."
        for c in _delta_chunks(text):
            yield c

    fake_get_setting = _make_fake_get_setting({"agent_tool_repeat_limit": 9999, "agent_stuck_rounds": 9999})
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", _distinct_stream)

    async def _tool(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        return "ok", {"exit_code": 0, "output": ""}

    monkeypatch.setattr(agent_loop, "execute_tool_block", _tool)
    monkeypatch.setattr(agent_loop, "get_setting", fake_get_setting)
    monkeypatch.setattr(settings_module, "get_setting", fake_get_setting)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(tool_index_module, "get_tool_index", lambda: None)

    async def _consume():
        out = []
        async for chunk in agent_loop.stream_agent_loop(
            endpoint_url="http://localhost:11434", model="test-model",
            messages=[{"role": "user", "content": "long task"}],
            owner="owner-1", session_id="sess-sc5-cont",
            max_rounds=3, allow_round_continuation=True,
        ):
            out.append(chunk)
        return out

    events = await asyncio.wait_for(_consume(), timeout=15)
    cont = _typed_events(events, "round_ceiling_continuation")
    assert cont, "assembled path must emit round_ceiling_continuation"
    assert cont[0]["extended_to"] == 3 + CONTINUATION_ROUND_BATCH
