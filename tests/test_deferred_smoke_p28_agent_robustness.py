"""Phase 37 automated re-encoding of Phase 28's deferred live smoke.

Source of the deferred property: `.planning/phases/28-agent-loop-execution-robustness/
28-VERIFICATION.md`, "Human Verification Required" (28-06-PLAN.md Task 2). That
checkpoint asked a human to run a LIVE server against a real model host and:

  1. trigger a genuinely hanging tool and confirm the SSE turn is timed out
     (not hung) with `exit_code == 124`;
  2. dump over-limit tool output and confirm it is truncated in-chat with a
     retrievable id, and that a cross-session/cross-owner request for that id
     is denied;
  3. force a real mid-stream host drop and confirm the stream retries with
     backoff and recovers, or ends with exactly one terminal error; and
  4. confirm Council is unaffected.

This file elevates above the existing hermetic proof in
`tests/test_phase28_integration.py`: that suite already drives the assembled
`stream_agent_loop`/`stream_llm_with_fallback`/`tool_output_store` paths, but
its wall-clock case (`test_runaway_tool_is_timed_out_sc5`) is, like this one,
already backed by a REAL `asyncio.sleep` racing a REAL monotonic deadline —
this file makes that GENUINENESS an explicit, load-bearing property under
test (never a monkeypatched/fake-elapsed clock: no `loop.time()` return value
is stubbed and no fake "elapsed" counter substitutes for wall-clock time) and
pairs it with a fresh assertion surface (owner/session output-store scoping,
mid-stream retry/backoff, and a direct Council-isolation check) rather than
merely duplicating the existing suite's cases.

Hermetic: no real model host, DB, or network. Every async drive is bounded by
an outer `asyncio.wait_for(..., timeout=15)` hang-safety ceiling so a
regression fails loud in CI instead of hanging the run.
"""
import asyncio
import re
from pathlib import Path

from core.constants import MID_STREAM_RETRY_MAX_ATTEMPTS

# Reuse the phase-shipped offline stream_agent_loop harness seams (the
# import-stub dance that makes src.agent_loop importable without a live DB
# lives in this module; every Phase 28 agent-loop test reuses it — these are
# plain helper functions, not pytest fixtures). Same pattern as
# tests/test_phase28_integration.py and tests/test_agent_loop_tool_timeout.py.
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
# Inline offline driver for the assembled stream_agent_loop path.
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
        session_id="sess-p37-03",
        max_rounds=len(rounds) + 2,
    )
    default_kwargs.update(loop_kwargs)

    async def _consume():
        out = []
        async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
            out.append(chunk)
        return out

    # Outer hang-safety ceiling: a timeout regression must fail LOUD, never
    # hang the test run.
    return await asyncio.wait_for(_consume(), timeout=15)


# ---------------------------------------------------------------------------
# Deferred property 1 — a GENUINELY hanging tool is cancelled at a real,
# short wall-clock deadline (Human Verification item 1 / GRAY-AGT-01).
# ---------------------------------------------------------------------------
async def test_genuinely_hanging_tool_is_cancelled_at_real_wall_clock(monkeypatch):
    # The tool never checks a clock or a flag — it just genuinely sleeps far
    # past the deadline below. Nothing here fakes elapsed time: the deadline
    # inside src/agent_loop.py is computed from a REAL
    # `asyncio.get_running_loop().time()` call, and this sleep is a REAL
    # `asyncio.sleep`. If the wall-clock mechanism regressed to a no-op, this
    # test would genuinely hang and be caught by the outer wait_for(timeout=15)
    # ceiling rather than silently reporting a false pass.
    async def _genuinely_hanging_tool(block, session_id=None, disabled_tools=None, owner=None, progress_cb=None):
        await asyncio.sleep(5.0)  # far past the tiny real wall-clock below
        return "SHOULD NOT REACH", {"output": "SHOULD NOT REACH", "exit_code": 0}

    # A small REAL wall-clock (seconds), not a stubbed clock return value.
    monkeypatch.setattr(agent_loop, "TOOL_WALL_CLOCK_TIMEOUT_SECONDS", 1.0)

    rounds = [
        _delta_chunks("```bash\nsleep 999\n```"),        # round 1: the hanging tool
        _delta_chunks("Turn continued after the timeout."),  # round 2: normal answer
    ]
    events = await _drive_agent_loop(monkeypatch, rounds, tool_override=_genuinely_hanging_tool)

    # The assembled turn completed (round 2 streamed) — it did NOT hang.
    assert "Turn continued after the timeout." in "".join(_extract_deltas(events))
    # The bash tool surfaced a timeout result (exit_code 124); the
    # would-be success output from the hung sleep never reached the stream.
    bash_out = [e for e in _typed_events(events, "tool_output") if e.get("tool") == "bash"]
    assert bash_out and bash_out[0].get("exit_code") == 124, f"expected timeout 124: {bash_out}"
    assert "SHOULD NOT REACH" not in "".join(events)


# ---------------------------------------------------------------------------
# Deferred property 2 — over-limit tool output is truncated with a
# retrieval id; the full output is retrievable ONLY for the owning
# (session_id, owner) (Human Verification item 2 / GRAY-AGT-02).
# ---------------------------------------------------------------------------
def test_truncated_output_store_full_text_is_owner_session_scoped(monkeypatch, tmp_path):
    from src import tool_output_store
    from src.tool_execution import _truncate_with_indicator, MAX_OUTPUT_CHARS

    # Redirect the store's on-disk root to an isolated tmp_path so this test
    # never touches the real DATA_DIR/tool_output — same seam
    # tests/test_tool_output_store.py already uses (_STORE_DIR is computed
    # from core.constants.DATA_DIR at import time, so patching DATA_DIR
    # after import would not move an already-bound _STORE_DIR).
    monkeypatch.setattr(tool_output_store, "_STORE_DIR", tmp_path)

    session_id, owner = "sess-p37-03-store", "owner-alice"
    full = "L" + ("x" * (MAX_OUTPUT_CHARS + 5000)) + "END"

    indicated = _truncate_with_indicator(full, MAX_OUTPUT_CHARS, session_id=session_id, owner=owner)

    # Model-visible text is truncated AND carries a resolvable retrieval id —
    # never a silent cut.
    assert len(indicated) < len(full)
    m = re.search(r"full output: ([0-9a-f]+)\]", indicated)
    assert m, f"no retrieval id in indicator: {indicated[-120:]!r}"
    output_id = m.group(1)

    # The owning (session_id, owner) retrieves the FULL, byte-identical text.
    got = tool_output_store.get(output_id, session_id=session_id, owner=owner)
    assert got == full, "owner/session retrieval must return the full untruncated text"

    # A different OWNER (same session) is denied.
    assert tool_output_store.get(output_id, session_id=session_id, owner="owner-mallory") is None
    # A different SESSION (same owner) is denied.
    assert tool_output_store.get(output_id, session_id="sess-other", owner=owner) is None
    # A different session AND owner is denied.
    assert tool_output_store.get(output_id, session_id="sess-other", owner="owner-mallory") is None


def test_output_store_put_get_round_trip_and_cross_owner_denial(monkeypatch, tmp_path):
    # A second, more direct put()/get() proof against the raw store API
    # (rather than going through _truncate_with_indicator), pinning the
    # store's own contract independent of the truncation call site.
    from src import tool_output_store

    monkeypatch.setattr(tool_output_store, "_STORE_DIR", tmp_path)

    full_text = "full tool output\n" * 10_000
    output_id = tool_output_store.put(full_text, session_id="s1", owner="alice")

    assert output_id and output_id.isalnum()
    assert tool_output_store.get(output_id, session_id="s1", owner="alice") == full_text
    assert tool_output_store.get(output_id, session_id="s1", owner="bob") is None
    assert tool_output_store.get(output_id, session_id="s2", owner="alice") is None


# ---------------------------------------------------------------------------
# Deferred property 3 — a mid-stream error AFTER content starts retries with
# bounded backoff and recovers, or emits exactly one terminal error — never a
# silent break (Human Verification item 3 / GRAY-AGT-05).
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
        # No-op sleep keeps this hermetic test fast; the backoff VALUE is
        # still recorded and asserted below so the mechanism itself (not
        # just the outcome) is proven.
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

    return asyncio.run(asyncio.wait_for(run(), timeout=15)), state["n"], backoffs


def test_mid_stream_drop_retries_and_recovers(monkeypatch):
    # Attempt 1: real content emitted, THEN a mid-stream error. Attempt 2:
    # clean recovery on a retry of the SAME candidate.
    attempts = [
        ['data: {"delta": "partial-"}\n\n', 'event: error\ndata: {"status": 503, "text": "reset"}\n\n'],
        ['data: {"delta": "recovered-content"}\n\n', "data: [DONE]\n\n"],
    ]
    chunks, calls, backoffs = _drive_fallback(monkeypatch, attempts)
    joined = "".join(chunks)

    assert "recovered-content" in joined, f"no recovery: {chunks}"
    assert not chunks[-1].startswith("event: error"), f"ended on error: {chunks}"
    # Exactly one retry of the SAME candidate (2 total stream_llm calls), and
    # exactly one backoff sleep — bounded, not a storm.
    assert calls == 2, f"expected exactly one retry (2 calls), got {calls}"
    assert len(backoffs) == 1
    assert MID_STREAM_RETRY_MAX_ATTEMPTS >= 1


def test_mid_stream_drop_exhausted_emits_exactly_one_terminal_error(monkeypatch):
    # Every attempt emits content then breaks — retries exhaust
    # MID_STREAM_RETRY_MAX_ATTEMPTS and the mechanism must forward exactly
    # ONE terminal error, never a silent truncation and never a duplicate.
    one_attempt = ['data: {"delta": "partial-"}\n\n', 'event: error\ndata: {"status": 503, "text": "reset"}\n\n']
    attempts = [one_attempt] * (MID_STREAM_RETRY_MAX_ATTEMPTS + 2)

    chunks, calls, backoffs = _drive_fallback(monkeypatch, attempts)

    error_chunks = [c for c in chunks if c.startswith("event: error")]
    assert len(error_chunks) == 1, f"expected exactly one terminal error, got {len(error_chunks)}: {chunks}"
    assert chunks[-1].startswith("event: error"), f"turn must end on the single terminal error: {chunks}"
    # Bounded: at most MID_STREAM_RETRY_MAX_ATTEMPTS retries were attempted
    # (i.e. MID_STREAM_RETRY_MAX_ATTEMPTS + 1 total stream_llm calls to the
    # SAME candidate), never an unbounded retry storm.
    assert calls == MID_STREAM_RETRY_MAX_ATTEMPTS + 1, (
        f"expected {MID_STREAM_RETRY_MAX_ATTEMPTS + 1} calls (bounded retries), got {calls}"
    )
    assert len(backoffs) == MID_STREAM_RETRY_MAX_ATTEMPTS


def test_pre_content_failure_still_falls_through_to_next_candidate(monkeypatch):
    # Regression guard: a PRE-content failure (no delta emitted yet) must
    # keep using the ORIGINAL next-candidate fallback behavior — the
    # GRAY-AGT-05 mid-stream retry only applies AFTER content has started.
    import src.llm_core as llm_core

    calls = []

    async def fake_stream(url, model, messages, **kw):
        calls.append(model)
        if model == "primary":
            yield 'event: error\ndata: {"status": 503, "text": "down"}\n\n'
            return
        yield 'data: {"delta": "fallback-content"}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_sleep(secs):
        pass

    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    monkeypatch.setattr(llm_core.asyncio, "sleep", fake_sleep)

    async def run():
        out = []
        async for c in llm_core.stream_llm_with_fallback(
            [("u1", "primary", {}), ("u2", "fallback", {})],
            [{"role": "user", "content": "hi"}],
        ):
            out.append(c)
        return out

    chunks = asyncio.run(asyncio.wait_for(run(), timeout=15))
    joined = "".join(chunks)

    assert calls == ["primary", "fallback"], f"expected fall-through to the next candidate: {calls}"
    assert "fallback-content" in joined
    assert not any(c.startswith("event: error") for c in chunks), (
        f"pre-content fallback must not surface the primary's swallowed error: {chunks}"
    )


# ---------------------------------------------------------------------------
# Deferred property 4 — Council (seat_runner) is unchanged by the Phase 28
# mid-stream mechanism: it still calls stream_llm directly, never
# stream_llm_with_fallback (Human Verification item 4).
# ---------------------------------------------------------------------------
def test_council_seat_runner_still_calls_stream_llm_directly():
    seat_runner_path = Path(__file__).resolve().parent.parent / "src" / "council" / "seat_runner.py"
    source = seat_runner_path.read_text(encoding="utf-8")

    # Positive assertion: seat_runner imports/calls stream_llm directly.
    assert "from src.llm_core import stream_llm" in source or "import stream_llm" in source, (
        "seat_runner.py must still import stream_llm directly"
    )
    assert re.search(r"\bstream_llm\s*\(", source), (
        "seat_runner.py must still call stream_llm(...) directly"
    )
    # It must NOT have picked up the Phase 28 fallback/retry wrapper — Council
    # stays isolated from the mid-stream retry mechanism under test here.
    assert "stream_llm_with_fallback" not in source, (
        "seat_runner.py must NOT call stream_llm_with_fallback — Council isolation broken"
    )


# ---------------------------------------------------------------------------
# RESIDUAL (documented, non-blocking) — what this file does NOT cover.
# ---------------------------------------------------------------------------
RESIDUAL_NOTE = """
RESIDUAL — not covered in-process by this file, and not automatable here:

  1. A REAL model host + a REAL network mid-stream drop delivered through a
     LIVE SSE turn (an actual killed/flaky HTTP connection mid-response, not
     a scripted fake async generator). This file proves the
     stream_llm_with_fallback retry/backoff STATE MACHINE exhaustively
     in-process (recovery, exhaustion-to-one-terminal-error, and the
     pre-content-fallback regression guard); it cannot prove a real host's
     TCP/HTTP failure mode reaches that state machine identically to the
     scripted one.
  2. The "not measurably slower" Council performance judgment (Human
     Verification item 4's perf half) — this file proves the CORRECTNESS
     half (seat_runner.py still calls stream_llm directly, never
     stream_llm_with_fallback) exhaustively; a live perf/UX judgment is out
     of scope for a hermetic unit/integration test.

Both residuals are non-blocking: the phase-28 mechanism itself is fully
proven in-process here and in tests/test_phase28_integration.py. The
live-host flavor of properties 1-3 (a genuinely hanging tool, real over-limit
output, and a real network drop, all delivered through a live server SSE
turn) is routed to a future live-smoke pass (Phase 38/39), not fabricated or
silently skipped here.
"""
