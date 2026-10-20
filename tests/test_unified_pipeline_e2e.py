"""Phase 7 UNIFY-04/UNIFY-05 capstone (D-01, D-10).

Drives a real code-producing request through the Council path — the one path
where curation and verification visibly co-occur on one request — with the
fresh-context flag opted ON, and asserts (via emitted signal AND shared-core
spy, per D-02) that BOTH curation AND verification demonstrably occurred:

  - Case A: a clean answer, curated (forced drop) AND verified with a real
    sandbox execution (verify_execute=True), no repair needed.
  - Case B: a broken answer, curated (forced drop), driven through exactly
    one grounded repair round of the shared verify gate (verify_execute=False),
    and accepted only after the re-verify genuinely passes.

Later plans append the non-regression no-op assertions to this SAME file
(D-10).

Council harness helpers below are copied — not imported — from
tests/test_council_fresh_context_integration.py, matching the repo's
copy-don't-import convention (Pitfall 3).
"""
from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

from src.answer_pipeline import run_pipeline
from src.council import fresh_context as fc
from src.council import seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat, SeatResult
from src import verified_gate

# ---------------------------------------------------------------------------
# Chat harness import-stub dance (copied from
# tests/test_agent_loop_verify_gate.py lines 27-98) -- must run AFTER the
# Council/Answer imports above (they need no DB stubs) and BEFORE
# `import src.agent_loop`. Stubs ONLY the heavy DB/IO deps so src.agent_loop
# can be imported and driven without a live database. `verified_gate` is
# already imported above (`from src import verified_gate`) -- reused here,
# not re-imported, to avoid a duplicate binding (ruff F811).
# ---------------------------------------------------------------------------
_MOCKED_IMPORTS = [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    'core.models', 'core.database',
]
_INJECTED_IMPORT_STUBS = {}


def _drop_module_if_same(name, expected):
    if sys.modules.get(name) is expected:
        sys.modules.pop(name, None)
    parent_name, _, attr = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, "__dict__", {}).get(attr) is expected:
        delattr(parent, attr)


# Mock heavy dependencies before importing. Only clean up stubs this file
# created so pre-existing conftest/pytest modules keep their intended state.
for _mod in _MOCKED_IMPORTS:
    if _mod not in sys.modules:
        _stub = MagicMock()
        sys.modules[_mod] = _stub
        _INJECTED_IMPORT_STUBS[_mod] = _stub

try:
    import src.agent_loop as agent_loop
    import src.settings as settings_module
    import src.tool_index as tool_index_module
finally:
    # Deliberately keep `src.agent_loop` (and src.settings, src.tool_index)
    # live in sys.modules -- monkeypatch targets below patch attributes on
    # these MODULE OBJECTS directly, never via a string path (a string path
    # would force importlib.import_module to re-import a second, unpatched
    # copy the moment it is missing from sys.modules -- Phase 6 war story).
    # Only clean up the injected heavy-dependency stubs so they don't leak
    # into other test modules.
    for _mod, _stub in _INJECTED_IMPORT_STUBS.items():
        _drop_module_if_same(_mod, _stub)

PROMPT = "What should we do?"


def _two_layer_cfg(persona: str = "") -> CouncilConfig:
    """Minimal 2-layer council: one panelist consumed by one synthesizer."""
    return CouncilConfig(seats=[
        Seat(id="p1", role="panelist", persona=persona, consumes=[]),
        Seat(id="chief", role="synthesizer", persona=persona, consumes=["p1"]),
    ])


def _fan_in_cfg(n: int, persona: str = "") -> CouncilConfig:
    """A synthesizer consuming `n` independent panelists — used to force a
    drop when `n` exceeds the curated window's max_items (D-03: n=10
    panelists / window=1000 is the council-integration-proven drop ratio —
    NOT the 50-item unit ratio from test_fresh_context.py)."""
    panelists = [Seat(id=f"p{i}", role="panelist", persona=persona, consumes=[])
                 for i in range(n)]
    chief = Seat(id="chief", role="synthesizer", persona=persona,
                 consumes=[p.id for p in panelists])
    return CouncilConfig(seats=panelists + [chief])


def _make_stubs(monkeypatch, scripted: dict[str, list[str]] | None = None,
                default: str = "canned answer, short and simple."):
    """Patch the model seam (`_resolve` + `llm_call_async` + `stream_llm`).

    `scripted` maps model (== seat.id) to a list of canned answers consumed IN
    ORDER across successive calls to that model — this is what lets Case B
    script two different synthesizer answers (bad, then good). Once a model's
    list is exhausted (or it was never scripted), `default` is used, so a
    no-op patch never silently hits the real network.

    Returns `captured`: model -> list of the exact `messages` arg each call
    received, in call order.
    """
    captured: dict[str, list[list[dict]]] = {}
    scripted = {k: list(v) for k, v in (scripted or {}).items()}

    def _next(model: str) -> str:
        queue = scripted.get(model)
        if queue:
            return queue.pop(0)
        return default

    def fake_resolve(seat, owner):
        return (f"http://fake/{seat.id}", seat.id, {})

    async def fake_llm_call_async(url, model, messages, temperature=1.0, headers=None, **kw):
        captured.setdefault(model, []).append(messages)
        return _next(model)

    async def fake_stream_llm(url, model, messages, temperature=1.0, headers=None, **kw):
        captured.setdefault(model, []).append(messages)
        content = _next(model)
        yield f"data: {json.dumps({'delta': content})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(seat_runner, "_resolve", fake_resolve)
    import src.llm_core as llm_core_mod
    monkeypatch.setattr(llm_core_mod, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr(llm_core_mod, "stream_llm", fake_stream_llm)
    return captured


async def _run(cfg: CouncilConfig, prompt: str = PROMPT, **kw) -> list[dict]:
    """Drain run_council's async generator into a plain list of events."""
    events = []
    async for ev in run_council(prompt, cfg, **kw):
        events.append(ev)
    return events


def _user_content(messages: list[dict]) -> str:
    return next(m["content"] for m in messages if m["role"] == "user")


def _retrieve(q, k):
    return [{"source": "doc1", "text": "The capital of France is Paris."}]


def _approve_review(question, draft, ctx):
    return {"approved": True, "critique": "ok"}


def _scripted_model(scripted):
    calls = {"n": 0, "prompts": []}

    def model_fn(prompt):
        calls["prompts"].append(prompt)
        out = scripted[min(calls["n"], len(scripted) - 1)]
        calls["n"] += 1
        return out

    return model_fn, calls


class TestUnifiedPipelineE2E:
    """UNIFY-04 capstone: one real code-producing Council request, flag ON,
    proving curation AND verification both occurred — via emitted signal AND
    shared-core module-object spy (D-02, UNIFY-03 anti-drift)."""

    async def test_case_a_clean_pass_curated_and_verified_execute_true(self, monkeypatch):
        n = 10
        window = 1000
        cfg = _fan_in_cfg(n)
        clean_answer = (
            "Here is a small helper.\n\n"
            "```python\n"
            "def add(a, b):\n"
            "    return a + b\n\n"
            "print(add(2, 3))\n"
            "```\n"
        )
        _make_stubs(
            monkeypatch,
            scripted={"chief": [clean_answer]},
            default="panelist answer, moderately sized content here.",
        )

        # curate_for spy — patched on the MODULE OBJECT (src.council.fresh_context),
        # not a string path, so the orchestrator's qualified _fresh.curate_for
        # reference observes the spy (Phase 6 war story).
        real_curate_for = fc.curate_for
        curate_calls = {"count": 0}

        def spy_curate_for(*args, **kwargs):
            curate_calls["count"] += 1
            return real_curate_for(*args, **kwargs)

        monkeypatch.setattr(fc, "curate_for", spy_curate_for)

        # verified_gate.evaluate spy — same module-object patch pattern, mirrors
        # TestSharedCoreUsage.spy_evaluate.
        real_evaluate = verified_gate.evaluate
        evaluate_calls = {"count": 0}

        def spy_evaluate(candidate, *, execute=False):
            evaluate_calls["count"] += 1
            return real_evaluate(candidate, execute=execute)

        monkeypatch.setattr(verified_gate, "evaluate", spy_evaluate)

        events = await _run(
            cfg, fresh_context=True, context_window=window,
            verify=True, verify_repair=True, verify_execute=True,
        )

        # Curation half: signal + spy.
        curated_events = [e for e in events
                          if e["type"] == "context_curated" and e["seat_id"] == "chief"]
        assert len(curated_events) == 1
        ev = curated_events[0]
        assert isinstance(ev["dropped"], int) and ev["dropped"] > 0
        assert len(ev["dropped_ids"]) == ev["dropped"]
        assert ev["window"] == window
        assert curate_calls["count"] >= 1

        # Verification half: signal + spy — exactly one initial stage, no repair,
        # real execution (ran is True), not just syntax-only.
        verification_events = [e for e in events if e["type"] == "verification"]
        assert len(verification_events) == 1
        v = verification_events[0]
        assert v["stage"] == "initial"
        assert not any(e["stage"] == "repair" for e in verification_events)
        assert v["has_code"] is True
        assert v["ran"] is True
        assert v["passed"] is True
        assert evaluate_calls["count"] == 1

        # Outcome.
        done = next(e for e in events if e["type"] == "done")
        assert done["verified"] is True
        assert done["final"] == clean_answer

    async def test_case_b_broken_then_repaired_curated_and_verified_execute_false(self, monkeypatch):
        n = 10
        window = 1000
        cfg = _fan_in_cfg(n)
        bad_answer = "Here is my answer:\n```python\ndef broken(:\n    pass\n```"
        good_answer = "The corrected answer, now with no code at all."
        _make_stubs(
            monkeypatch,
            scripted={"chief": [bad_answer, good_answer]},
            default="panelist answer, moderately sized content here.",
        )

        # verified_gate.evaluate spy — proves the shared core ran on both the
        # initial and repair-recheck calls.
        real_evaluate = verified_gate.evaluate
        evaluate_calls = {"count": 0}

        def spy_evaluate(candidate, *, execute=False):
            evaluate_calls["count"] += 1
            return real_evaluate(candidate, execute=execute)

        monkeypatch.setattr(verified_gate, "evaluate", spy_evaluate)

        # curate_for spy — proves curation ran on this flag-on run via spy too,
        # not signal alone, matching Case A / Truth 4 (D-02).
        real_curate_for = fc.curate_for
        curate_calls = {"count": 0}

        def spy_curate_for(*args, **kwargs):
            curate_calls["count"] += 1
            return real_curate_for(*args, **kwargs)

        monkeypatch.setattr(fc, "curate_for", spy_curate_for)

        events = await _run(
            cfg, fresh_context=True, context_window=window,
            verify=True, verify_repair=True, verify_execute=False,
        )

        # Curation half: signal + spy — the repair round must NOT re-curate or
        # re-emit (only one context_curated event across the whole run).
        curated_events = [e for e in events
                          if e["type"] == "context_curated" and e["seat_id"] == "chief"]
        assert len(curated_events) == 1
        assert isinstance(curated_events[0]["dropped"], int) and curated_events[0]["dropped"] > 0
        assert curate_calls["count"] >= 1

        # Verification half: exactly two stages (initial + repair, no third
        # round), initial fails, repair's OWN verdict passes.
        verification_events = [e for e in events if e["type"] == "verification"]
        assert len(verification_events) == 2
        stages = {e["stage"] for e in verification_events}
        assert stages == {"initial", "repair"}
        initial_v = next(e for e in verification_events if e["stage"] == "initial")
        repair_v = next(e for e in verification_events if e["stage"] == "repair")
        assert initial_v["passed"] is False
        assert repair_v["passed"] is True
        assert evaluate_calls["count"] >= 2

        # Outcome.
        done = next(e for e in events if e["type"] == "done")
        assert done["verified"] is True
        assert done["final"] == good_answer


# ---------------------------------------------------------------------------
# Chat harness (copied — not imported — from
# tests/test_agent_loop_verify_gate.py, matching the repo's copy-don't-import
# convention). Drives the real `stream_agent_loop` end-to-end, offline.
# ---------------------------------------------------------------------------

def _delta_chunks(text: str) -> list:
    """Build the SSE chunks for a single round: one text delta then [DONE].

    Chat's chunk shape is `data: {"delta": "..."}` -- NOT Council/Answer's
    plain-string `llm_call_async` return. Do not copy that harness's format.
    """
    return [f'data: {json.dumps({"delta": text})}\n\n', 'data: [DONE]\n\n']


def _extract_deltas(events: list) -> list:
    """Pull the ordered list of assistant text deltas out of an SSE stream."""
    out = []
    for e in events:
        if not e.startswith("data: ") or e.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(e[len("data: "):].strip())
        except json.JSONDecodeError:
            continue
        if "delta" in data:
            out.append(data["delta"])
    return out


def _agent_step_rounds(events: list) -> list:
    """Ordered list of `round` values from every `agent_step` SSE event."""
    out = []
    for e in events:
        if not e.startswith("data: ") or e.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(e[len("data: "):].strip())
        except json.JSONDecodeError:
            continue
        if data.get("type") == "agent_step":
            out.append(data["round"])
    return out


def _make_fake_get_setting(overrides=None):
    """Fake `get_setting(key, default)` for offline, deterministic runs.

    `agent_syntax_gate` resolves True and `agent_verify_execute` resolves
    False unless explicitly overridden by the caller; every other key
    returns its own supplied default (never touches disk).
    """
    overrides = overrides or {}

    def fake_get_setting(key, default=None):
        if key in overrides:
            return overrides[key]
        if key == "agent_syntax_gate":
            return True
        if key == "agent_verify_execute":
            return False
        return default

    return fake_get_setting


def _make_fake_stream(rounds):
    """rounds: list of chunk-lists, one list per LLM call (= one round each).

    The Nth call to `stream_llm_with_fallback` consumes `rounds[N]`; if the
    loop asks for more rounds than were scripted, the LAST scripted round is
    replayed (keeps tests robust to an extra round without needing to script
    every possible continuation).
    """
    state = {"n": 0}

    async def fake_stream_llm_with_fallback(*_args, **_kwargs):
        idx = min(state["n"], len(rounds) - 1)
        state["n"] += 1
        for chunk in rounds[idx]:
            yield chunk

    return fake_stream_llm_with_fallback


async def _fake_execute_tool_block(block, session_id=None, disabled_tools=None,
                                    owner=None, progress_cb=None):
    """Stub for `execute_tool_block` -- no test in this file exercises real
    tool execution (tool_blocks stays empty via session_id=None), so this
    default is a pure addition, never a behavior change."""
    return "tool executed (stub)", {"exit_code": 0, "output": ""}


async def run_chat(monkeypatch, rounds, *, settings=None, message_log=None, **loop_kwargs):
    """Drive the real `stream_agent_loop` end-to-end, offline.

    `rounds` is a list of chunk-lists (one list of SSE-shaped strings per
    scripted LLM round). Returns the ordered list of yielded SSE strings."""
    fake_get_setting = _make_fake_get_setting(settings)

    # LOAD-BEARING: the monkeypatch target is src.agent_loop.stream_llm_with_fallback
    # -- the name bound INTO the agent_loop namespace at import time
    # (src/agent_loop.py:19, `from src.llm_core import ... stream_llm_with_fallback`)
    # and called at src/agent_loop.py:1775. Patched via the live `agent_loop`
    # MODULE OBJECT (not a "src.agent_loop.stream_llm_with_fallback" string
    # path): a string path would make monkeypatch call
    # importlib.import_module("src.agent_loop"), which -- since this file's
    # own cleanup step never drops "src.agent_loop" from sys.modules -- would
    # normally resolve back to this exact object, but relying on that string
    # form is fragile; the direct object reference is unambiguous (Phase 6
    # war story: a string-path patch silently re-imported a second,
    # unpatched copy of agent_loop and let a REAL network call reach
    # http://localhost:11434).
    _base_fake_stream = _make_fake_stream(rounds)

    async def _capturing_fake_stream(*args, **kwargs):
        if message_log is not None:
            # args[1] is `messages` (src/agent_loop.py:1793-1795, positional).
            message_log.append(list(args[1]))
        async for chunk in _base_fake_stream(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", _capturing_fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", _fake_execute_tool_block)

    # get_setting must be patched at BOTH sites:
    #  - agent_loop.get_setting: the name pre-bound into agent_loop's own
    #    namespace at import time (`from src.settings import get_setting`).
    #  - settings_module.get_setting (src.settings.get_setting): the source
    #    attribute itself, used by code that does a FRESH
    #    `from src.settings import get_setting` at call time.
    monkeypatch.setattr(agent_loop, "get_setting", fake_get_setting)
    monkeypatch.setattr(settings_module, "get_setting", fake_get_setting)

    # Offline seams: no live MCP/tool-index/DB stack required.
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(tool_index_module, "get_tool_index", lambda: None)

    default_kwargs = dict(
        endpoint_url="http://localhost:11434",
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        owner="owner-1",
        # Keep the auto-create-document fallback OFF by default (requires a
        # truthy session_id) -- this harness's scope is the answer-text
        # syntax/verify gate only.
        session_id=None,
        max_rounds=len(rounds) + 2,
    )
    default_kwargs.update(loop_kwargs)

    events = []
    async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
        events.append(chunk)
    return events


class TestNonRegressionNoOps:
    """UNIFY-05: each original entry point stays a structural no-op for its
    existing non-code callers with the flag off — proving unification is
    additive (no caller-visible regression) before Phase 8's default flip."""

    async def test_council_non_code_request_is_structural_no_op(self, monkeypatch):
        cfg = _two_layer_cfg()
        p1_answer = "p1's raw answer, short and simple."
        captured = _make_stubs(
            monkeypatch,
            scripted={"p1": [p1_answer],
                     "chief": ["The capital of France is Paris, no code here."]},
        )

        # Drive flag OFF (the current project default) — no fresh_context,
        # no context_window (Pitfall 4, D-07).
        events = await _run(cfg, fresh_context=False)

        # Structural no-op: verify gate runs (default verify=True) but never
        # meaningfully fires — prose-only verdict, no repair round.
        verification_events = [e for e in events if e["type"] == "verification"]
        assert len(verification_events) == 1
        v = verification_events[0]
        assert v["has_code"] is False
        assert v["passed"] is True
        assert not any(e["stage"] == "repair" for e in verification_events)

        done = next(e for e in events if e["type"] == "done")
        assert done["verified"] is True

        # Flag-off curation drops nothing: zero context_curated events.
        assert not any(e["type"] == "context_curated" for e in events)

        # Flag-off parity (byte-for-byte): the assembled synthesizer messages
        # equal the raw pre-unification _upstream_for concatenation — this
        # only proves the default-off path because the flag is genuinely OFF.
        chief_messages = captured["chief"][0]
        p1_result = SeatResult(seat_id="p1", name="", role="panelist",
                               ok=True, content=p1_answer)
        expected = seat_runner._build_messages(cfg.seat("chief"), PROMPT, [p1_result])
        assert chief_messages == expected

    def test_answer_pipeline_non_code_request_is_structural_no_op(self, monkeypatch):
        # verified_gate.evaluate spy — patched on the MODULE OBJECT (same shape
        # as TestSharedCoreUsage) so answer_pipeline's qualified `_vg.evaluate`
        # reference observes the spy; count==0 proves the shared gate is
        # genuinely never entered on this default (code_verify=False) path.
        real_evaluate = verified_gate.evaluate
        evaluate_calls = {"count": 0}

        def spy_evaluate(candidate, *, execute=False):
            evaluate_calls["count"] += 1
            return real_evaluate(candidate, execute=execute)

        monkeypatch.setattr(verified_gate, "evaluate", spy_evaluate)

        model_fn, calls = _scripted_model(["Paris. [doc1]"])
        r = run_pipeline("Capital of France?", model_fn=model_fn, retrieve_fn=_retrieve,
                         review_fn=_approve_review, context_window=None)

        # Structural no-op: the sandbox code gate (`if code_verify and
        # draft.strip():`) was never entered with the defaults (code_verify=False).
        assert "code_verified" not in r.orchestration
        assert "code_ran" not in r.orchestration
        assert r.orchestration == {"samples": 1, "structured": False}

        # The shared verify core was genuinely never invoked on this path.
        assert evaluate_calls["count"] == 0

        # The existing caller still gets the correct result.
        assert r.answer == "Paris. [doc1]"
        assert r.approved is True

    async def test_chat_non_code_request_is_structural_no_op(self, monkeypatch):
        # verified_gate.evaluate spy -- module-object patch (same shape as the
        # Council/Answer no-ops above); count==0 proves the shared gate never
        # engages for a non-code turn.
        real_evaluate = verified_gate.evaluate
        evaluate_calls = {"count": 0}

        def spy_evaluate(candidate, *, execute=False):
            evaluate_calls["count"] += 1
            return real_evaluate(candidate, execute=execute)

        monkeypatch.setattr(verified_gate, "evaluate", spy_evaluate)

        # Plain-prose turn -- NO code fence at all (Pitfall 3: a bare
        # ```python fence would be intercepted by parse_tool_blocks as an
        # execution tool call before the gate is ever reached, changing
        # which path is under test).
        events = await run_chat(
            monkeypatch,
            [_delta_chunks("The capital of France is Paris.")],
        )

        deltas = "".join(_extract_deltas(events))

        # Structural no-op: no repair round fired, no gate fix-up note, no
        # error event.
        assert _agent_step_rounds(events) == []
        assert "Caught a Python syntax error" not in deltas
        # planner-discipline-allow: Caught a Python syntax error
        assert not any(e.startswith("event: error") for e in events)

        # The shared gate never engaged.
        assert evaluate_calls["count"] == 0

        # The existing caller still gets the correct prose answer.
        assert "Paris" in deltas
