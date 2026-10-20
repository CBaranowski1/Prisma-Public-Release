"""SEC-08 reporting honesty (Plan 11-04).

Plan 11-01 widened `src/council/verify.py::verify_answer` so `passed` is now
non-blocking (True for BOTH genuinely-passed AND unverified code, e.g. a lone
```js block) and added an honest `verified` key (True only when every code
block present was genuinely validated clean). If the two user/API-facing
reporting sites -- `src/council/orchestrator.py`'s "done" event and
`src/answer_pipeline.py`'s `orchestration["code_verified"]` -- kept reading
`passed`, an unverified answer would report `verified: True` to the end user
and the Phase-12 A/B harness, which is MORE dishonest than before this phase.

This file proves:
  1. Council: a lone-```js answer reports `done["verified"] is False` while
     `verdict["passed"] is True` and NO repair round fires (non-blocking).
  2. Answer pipeline: the same lone-```js draft reports
     `orchestration["code_verified"] is False` while the answer stays
     `approved` is NOT forced False purely by the unverified language (no
     repair round fires either -- `needs_repair` reads `passed`, which stays
     True).
  3. Chat scope boundary (D-06): `src/agent_loop.py` still contains its
     `"```py"` entry-trigger pre-filter substring, unchanged -- proving the
     Chat/agent_loop gap is a deliberate, asserted deferral, not a silent
     drop.

Council harness helpers below are copied -- not imported -- from
tests/test_unified_pipeline_e2e.py / tests/test_council_fresh_context_integration.py,
matching the repo's copy-don't-import convention (Pitfall 3, RESEARCH.md).

Fixture language note (Phase 41 Plan 02): the unverified-language fixture
below uses `js` rather than `sql`. Before Phase 41-02, `sqlglot` was not
installed, so a lone ```sql``` block always landed in `unverified_languages`
in every dev environment (11-RESEARCH.md). Phase 41-02 installs `sqlglot`
(D-09 SQL side) to genuinely activate `verify.py`'s dormant SQL validator
tier -- so a syntactically-valid `SELECT * FROM users;` now VALIDATES
cleanly and is no longer unverified, which would silently invalidate this
file's entire premise. `js` is in `verify.py`'s `_NO_VALIDATOR_TAGS` (no
validator exists for it in ANY environment), so it deterministically proves
the same reporting-honesty contract this file has always been about,
independent of which optional SQL/exec dependencies happen to be installed.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.answer_pipeline import run_pipeline
from src.council import seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat

PROMPT = "Show me a query."

SQL_ONLY_ANSWER = (
    "Here is a snippet that answers your question.\n\n"
    "```js\n"
    "console.log('hello');\n"
    "```\n"
)


def _two_layer_cfg(persona: str = "") -> CouncilConfig:
    """Minimal 2-layer council: one panelist consumed by one synthesizer."""
    return CouncilConfig(seats=[
        Seat(id="p1", role="panelist", persona=persona, consumes=[]),
        Seat(id="chief", role="synthesizer", persona=persona, consumes=["p1"]),
    ])


def _make_stubs(monkeypatch, scripted: dict[str, list[str]] | None = None,
                default: str = "canned answer, short and simple."):
    """Patch the model seam (`_resolve` + `llm_call_async` + `stream_llm`).

    Copied from tests/test_unified_pipeline_e2e.py's `_make_stubs` (same
    signature/behavior) -- not imported, per the repo's copy-don't-import
    test-harness convention.
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


def _retrieve(q, k):
    return [{"source": "doc1", "text": "The users table has columns id, name."}]


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


class TestCouncilReportingHonesty:
    """An unverified-only (lone ```js) Council answer must report
    `done["verified"] is False` -- not the non-blocking `passed` value -- and
    must NOT trigger a repair round (non-blocking per D-02)."""

    async def test_sql_only_answer_reports_unverified_not_blocked(self, monkeypatch):
        cfg = _two_layer_cfg()
        _make_stubs(
            monkeypatch,
            scripted={"chief": [SQL_ONLY_ANSWER]},
            default="panelist answer, moderately sized content here.",
        )

        events = await _run(cfg, verify=True, verify_repair=True, verify_execute=False)

        verification_events = [e for e in events if e["type"] == "verification"]
        assert len(verification_events) == 1
        v = verification_events[0]
        # Non-blocking half (D-02): passed stays True, no repair fired.
        assert v["passed"] is True
        assert v["unverified_languages"] == ["js"]
        assert not any(e["stage"] == "repair" for e in verification_events)

        # Honesty half (SEC-08 reporting fix): the done-event's user/API-facing
        # "verified" flag must read the HONEST verdict.verified key, not the
        # non-blocking "passed" key -- an unverified JS answer is NOT verified.
        done = next(e for e in events if e["type"] == "done")
        assert v["verified"] is False
        assert done["verified"] is False
        assert done["final"] == SQL_ONLY_ANSWER


class TestAnswerPipelineReportingHonesty:
    """The same lone-```js draft must report
    `orchestration["code_verified"] is False` from the Answer pipeline's
    sandbox code gate, without a repair round firing."""

    def test_sql_only_draft_reports_code_unverified_not_blocked(self):
        model_fn, _ = _scripted_model([SQL_ONLY_ANSWER])

        r = run_pipeline(
            "Show me a query.",
            model_fn=model_fn,
            retrieve_fn=_retrieve,
            review_fn=_approve_review,
            code_verify=True,
            code_execute=False,
            context_window=None,
        )

        # Honesty half: code_verified must be False for unverified-only JS --
        # NOT sourced from the non-blocking `passed` key (which is True here).
        assert r.orchestration["code_verified"] is False

        # Non-blocking half (D-02): no repair transcript entry was needed
        # because `needs_repair` (sourced from `passed`, unchanged) never
        # fired for this unverified-but-not-failed draft.
        sandbox_entries = [t for t in r.transcript if t.get("role") == "sandbox"]
        assert sandbox_entries == []

        # The answer itself is not forced unapproved purely by an unverified
        # code language -- only a genuine sandbox `failed` or missing
        # grounding does that (grounding is present here).
        assert r.answer == SQL_ONLY_ANSWER


class TestChatScopeBoundaryUnchanged:
    """D-06 / research Open Question 2: Chat's (`src/agent_loop.py`) entry
    trigger into the verify gate is a SEPARATE pre-filter from Council/Answer
    (which call `_vg.evaluate` unconditionally). SEC-08 is deliberately out of
    scope for Chat this phase -- this test asserts the trigger is unchanged,
    making the deferral an enforced, visible fact rather than a silent gap."""

    def test_agent_loop_entry_trigger_still_gates_on_triple_backtick_py(self):
        agent_loop_path = Path(__file__).resolve().parent.parent / "src" / "agent_loop.py"
        text = agent_loop_path.read_text(encoding="utf-8")
        assert '"```py" in cleaned_round' in text
