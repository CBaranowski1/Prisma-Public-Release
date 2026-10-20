"""GRAY-AGT-01: in-loop tool wall-clock + fixed preamble string.

The tool-execution await (src/agent_loop.py) is wrapped in asyncio.wait_for so a
runaway/hung tool is cancelled with a clean timeout result instead of blocking
the SSE turn forever. The stale "60s timeout per tool" preamble is replaced by
text bound to the real TOOL_WALL_CLOCK_TIMEOUT_SECONDS constant.

Reuses the offline run_chat harness building blocks from
tests/test_agent_loop_verify_gate.py, but installs a SLOW execute_tool_block and
a tiny wall-clock so the timeout path is exercised deterministically.
"""
import asyncio

from tests.test_agent_loop_verify_gate import (
    agent_loop,
    settings_module,
    tool_index_module,
    _make_fake_stream,
    _make_fake_get_setting,
    _delta_chunks,
    _typed_events,
    _extract_deltas,
)


async def _run_chat_slow_tool(monkeypatch, rounds, *, wall_clock, sleep_secs, **loop_kwargs):
    """Drive stream_agent_loop offline with a slow execute_tool_block and a
    monkeypatched-tiny wall-clock. Returns the yielded SSE strings."""
    fake_get_setting = _make_fake_get_setting(loop_kwargs.pop("settings", None))
    fake_stream = _make_fake_stream(rounds)

    async def _slow_tool(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        # Sleeps past the (tiny) wall-clock so the outer wait_for trips.
        await asyncio.sleep(sleep_secs)
        return "slow tool done", {"output": "SHOULD NOT REACH", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", _slow_tool)
    monkeypatch.setattr(agent_loop, "get_setting", fake_get_setting)
    monkeypatch.setattr(settings_module, "get_setting", fake_get_setting)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(tool_index_module, "get_tool_index", lambda: None)
    # The load-bearing edit under test: a tiny wall-clock so the slow tool trips it.
    monkeypatch.setattr(agent_loop, "TOOL_WALL_CLOCK_TIMEOUT_SECONDS", wall_clock)

    default_kwargs = dict(
        endpoint_url="http://localhost:11434",
        model="test-model",
        messages=[{"role": "user", "content": "run a slow tool"}],
        owner="owner-1",
        session_id="sess-1",
        max_rounds=len(rounds) + 2,
    )
    default_kwargs.update(loop_kwargs)

    async def _consume():
        events = []
        async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
            events.append(chunk)
        return events

    # Outer ceiling: if the wall-clock regressed and the tool truly hangs, fail
    # LOUD (TimeoutError) instead of hanging CI forever.
    return await asyncio.wait_for(_consume(), timeout=15)


async def test_runaway_tool_is_timed_out_not_hung(monkeypatch):
    rounds = [
        _delta_chunks("```bash\nsleep 999\n```"),   # round 1: a hanging tool
        _delta_chunks("Done — the slow tool was bounded."),  # round 2: normal answer
    ]
    events = await _run_chat_slow_tool(monkeypatch, rounds, wall_clock=0.1, sleep_secs=5.0)

    # The turn completed (round 2 answer streamed) — it did NOT hang.
    deltas = "".join(_extract_deltas(events))
    assert "the slow tool was bounded" in deltas

    # The bash tool surfaced a timeout result (exit_code 124), proving the
    # in-loop wall-clock cancelled it rather than awaiting the 5s sleep.
    bash_outputs = [e for e in _typed_events(events, "tool_output") if e.get("tool") == "bash"]
    assert bash_outputs, f"no bash tool_output event: {[e for e in events if 'tool' in e]}"
    assert bash_outputs[0].get("exit_code") == 124, f"expected timeout exit_code 124: {bash_outputs[0]}"
    # And the SHOULD-NOT-REACH success output never made it into the stream.
    assert "SHOULD NOT REACH" not in "".join(events)


async def test_normal_tool_under_wall_clock_is_unaffected(monkeypatch):
    # A tool that finishes well within the wall-clock behaves normally (no
    # timeout result), proving the wrapper only fires on genuine overruns.
    rounds = [
        _delta_chunks("```bash\necho quick\n```"),
        _delta_chunks("Quick tool finished."),
    ]
    events = await _run_chat_slow_tool(monkeypatch, rounds, wall_clock=5.0, sleep_secs=0.01)
    bash_outputs = [e for e in _typed_events(events, "tool_output") if e.get("tool") == "bash"]
    assert bash_outputs and bash_outputs[0].get("exit_code") == 0
    assert "Quick tool finished." in "".join(_extract_deltas(events))


def test_preamble_reflects_real_wall_clock_not_60s():
    # GRAY-AGT-01: the stale hardcoded "60s timeout per tool" is gone and the
    # rules now show values derived from the enforced constants.
    from core.constants import TOOL_WALL_CLOCK_TIMEOUT_SECONDS, MAX_OUTPUT_CHARS

    rules = agent_loop._AGENT_RULES
    assert "60s timeout per tool" not in rules
    assert f"{TOOL_WALL_CLOCK_TIMEOUT_SECONDS // 60}min" in rules
    assert f"{MAX_OUTPUT_CHARS // 1000}K" in rules
    # _API_AGENT_RULES never carried the stale literal; confirm it stays clean.
    assert "60s timeout per tool" not in agent_loop._API_AGENT_RULES
