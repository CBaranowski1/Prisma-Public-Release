"""GRAY-AGT-03: bounded, opt-in, health-gated round-ceiling continuation.

Today when the `while round_num < max_rounds` ceiling is reached with a task
still mid-flight (the last round produced tool_blocks and their results were fed
back), control falls through to `_empty_response_fallback` + [DONE] — a silent
mid-task cutoff. This suite proves the replacement:

  (a) a HEALTHY long turn with `allow_round_continuation=True` emits
      `round_ceiling_continuation` events and runs PAST the original ceiling;
  (b) the DEFAULT (kwarg omitted / False) turn is unchanged — no event, stops
      at the original ceiling;
  (c) a STUCK/looping turn is NOT extended (the loop-breaker health signals gate
      continuation off);
  (d) the ABSOLUTE ceiling is enforced — at most MAX_CONTINUATION_BATCHES
      extensions, so it can never loop forever.

Reuses the offline run_chat substrate from tests/test_agent_loop_verify_gate.py
but installs custom scripted streams (distinct-vs-identical tool calls) and
settings overrides so the continuation logic is isolated from the unrelated
runaway backstop (agent_tool_repeat_limit) and stuck detector (agent_stuck_rounds).
"""
from core.constants import (
    MAX_CONTINUATION_BATCHES,
    CONTINUATION_ROUND_BATCH,
)

from tests.test_agent_loop_verify_gate import (
    agent_loop,
    settings_module,
    tool_index_module,
    _make_fake_get_setting,
    _delta_chunks,
    _typed_events,
    _extract_deltas,
)


def _distinct_tool_stream(n_tool_rounds, final_text="All steps complete."):
    """Each of the first `n_tool_rounds` LLM calls emits a DISTINCT bash tool
    call (distinct content → never a loop-breaker repeat, so `_stuck_rounds`
    stays 0 and healthy continuation is exercised); afterwards a plain-text
    answer that ends the turn. `state["n"]` records how many rounds actually ran.
    """
    state = {"n": 0}

    async def fake_stream(*_args, **_kwargs):
        i = state["n"]
        state["n"] += 1
        if i < n_tool_rounds:
            chunks = _delta_chunks(f"```bash\necho distinct-step-{i}\n```")
        else:
            chunks = _delta_chunks(final_text)
        for chunk in chunks:
            yield chunk

    return fake_stream, state


def _identical_tool_stream(n_repeat, final_text="Blocked — stopping."):
    """Emits the SAME bash call `n_repeat` times (identical content with no
    answer text → trips the loop-breaker's repeat detector), then a plain
    answer so the turn still terminates for the test."""
    state = {"n": 0}

    async def fake_stream(*_args, **_kwargs):
        i = state["n"]
        state["n"] += 1
        if i < n_repeat:
            chunks = _delta_chunks("```bash\necho SAME-COMMAND\n```")
        else:
            chunks = _delta_chunks(final_text)
        for chunk in chunks:
            yield chunk

    return fake_stream, state


async def _drive(monkeypatch, fake_stream, *, settings, **loop_kwargs):
    """Minimal offline driver for stream_agent_loop with a custom scripted
    stream (mirrors run_chat's monkeypatch seams, but lets us inject an
    arbitrary stream generator and count rounds via its closure state)."""
    fake_get_setting = _make_fake_get_setting(settings)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)
    monkeypatch.setattr(agent_loop, "get_setting", fake_get_setting)
    monkeypatch.setattr(settings_module, "get_setting", fake_get_setting)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(tool_index_module, "get_tool_index", lambda: None)

    default_kwargs = dict(
        endpoint_url="http://localhost:11434",
        model="test-model",
        messages=[{"role": "user", "content": "do a long multi-step task"}],
        owner="owner-1",
        session_id="sess-cont",  # truthy so bash tool_blocks execute via the stub
    )
    default_kwargs.update(loop_kwargs)

    events = []
    async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
        events.append(chunk)
    return events


# High repeat/stuck limits so the runaway backstop + stuck detector never
# interfere with the continuation-ceiling tests that WANT many distinct rounds.
_ISOLATE = {"agent_tool_repeat_limit": 9999, "agent_stuck_rounds": 9999}


async def test_healthy_long_turn_continues_past_ceiling(monkeypatch):
    # 5 distinct tool rounds then an answer, but a tiny ceiling of 3 → the turn
    # must extend past round 3 and reach the answer.
    fake_stream, state = _distinct_tool_stream(n_tool_rounds=5)
    events = await _drive(
        monkeypatch, fake_stream, settings=_ISOLATE,
        max_rounds=3, allow_round_continuation=True,
    )

    cont = _typed_events(events, "round_ceiling_continuation")
    assert cont, "expected at least one round_ceiling_continuation event"
    assert cont[0]["extended_to"] == 3 + CONTINUATION_ROUND_BATCH
    # It ran past the original ceiling and reached the scripted final answer.
    assert state["n"] > 3
    assert "All steps complete." in "".join(_extract_deltas(events))


async def test_default_off_stops_at_original_ceiling(monkeypatch):
    # Same script, but allow_round_continuation omitted (defaults False):
    # behavior must be unchanged — no continuation event, stops at round 3.
    fake_stream, state = _distinct_tool_stream(n_tool_rounds=5)
    events = await _drive(
        monkeypatch, fake_stream, settings=_ISOLATE,
        max_rounds=3,  # no allow_round_continuation
    )

    assert _typed_events(events, "round_ceiling_continuation") == []
    # Exactly the original ceiling of 3 LLM rounds ran — no extension.
    assert state["n"] == 3
    assert "All steps complete." not in "".join(_extract_deltas(events))


async def test_stuck_turn_is_not_extended(monkeypatch):
    # Identical tool calls trip the loop-breaker (agent_stuck_rounds=2). Even
    # with opt-in True, a non-converging turn must NOT be extended.
    fake_stream, state = _identical_tool_stream(n_repeat=8)
    settings = {"agent_tool_repeat_limit": 9999, "agent_stuck_rounds": 2}
    events = await _drive(
        monkeypatch, fake_stream, settings=settings,
        max_rounds=6, allow_round_continuation=True,
    )

    assert _typed_events(events, "round_ceiling_continuation") == [], (
        "a stuck/looping turn must never be extended"
    )


async def test_absolute_ceiling_is_enforced(monkeypatch):
    # Distinct tool calls that NEVER terminate (n_tool_rounds huge) with opt-in
    # True: continuation must fire at most MAX_CONTINUATION_BATCHES times and the
    # loop must stop at the hard ceiling, never spinning forever.
    fake_stream, state = _distinct_tool_stream(n_tool_rounds=10_000)
    events = await _drive(
        monkeypatch, fake_stream, settings=_ISOLATE,
        max_rounds=3, allow_round_continuation=True,
    )

    cont = _typed_events(events, "round_ceiling_continuation")
    assert len(cont) == MAX_CONTINUATION_BATCHES, (
        f"expected exactly {MAX_CONTINUATION_BATCHES} extensions, got {len(cont)}"
    )
    # Absolute ceiling = original max_rounds + MAX_CONTINUATION_BATCHES batches.
    expected_ceiling = 3 + MAX_CONTINUATION_BATCHES * CONTINUATION_ROUND_BATCH
    assert state["n"] == expected_ceiling, (
        f"expected exactly {expected_ceiling} rounds, got {state['n']}"
    )
    assert cont[-1]["extended_to"] == expected_ceiling
