"""Guard tests for src/answer_pipeline.py's opt-in context_window budget guard
(GUARD-02/GUARD-03): grounding is curated per model call (re-curated as the
grounding list grows across rounds), and carried-forward history strings
(prior draft, reviewer critique, structured plan) are distilled before they
enter the revise/repair/draft prompts.

Offline, stub-driven — no real LLM / brain. Mirrors
tests/test_answer_pipeline_orchestration.py's fixture style (_scripted_model
capture pattern); that file stays UNMODIFIED (GUARD-04 flag-off proof).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.answer_pipeline import run_pipeline, _HISTORY_CAP_DIVISOR, TARGET
from src.context_curation import fits, DEFAULT_WINDOW
from src import verified_gate


def _scripted_model(scripted):
    calls = {"n": 0, "prompts": []}

    def model_fn(prompt):
        calls["prompts"].append(prompt)
        out = scripted[min(calls["n"], len(scripted) - 1)]
        calls["n"] += 1
        return out

    return model_fn, calls


def _oversized_hits(prefix, n, size=300):
    return [{"source": f"{prefix}{i}", "text": "X" * size} for i in range(n)]


def _retrieve_growing(q, k):
    # Growing grounding across rounds (Pitfall 4): the initial retrieval returns
    # one oversized batch; the round-1 requery ("more") returns an ADDITIONAL
    # oversized batch with distinct source ids, so the raw grounding list keeps
    # growing across rounds and each round's curation must re-run, not reuse a
    # cached round-0 result.
    if q == "more":
        return _oversized_hits("extra", 6)
    return _oversized_hits("doc", 10)


def _reject_once_then_approve(requery=None):
    state = {"n": 0}

    def review_fn(question, draft, ctx):
        state["n"] += 1
        if state["n"] == 1:
            return {"approved": False, "critique": "needs more sources", "requery": requery}
        return {"approved": True, "critique": "ok"}

    return review_fn


def test_grounding_curated_per_call():
    # window << raw grounding size (10 x 300 chars = 3000 > 1200; after growth
    # 16 x 300 = 4800 > 1200) forces curate() to actually drop/distill, so a
    # prompt staying bounded proves curation ran, not that inputs happened to
    # already fit.
    context_window = 1200
    # TARGET() (imported above) is the single source of truth for this bound --
    # it already folds in _STATIC_OVERHEAD, which itself tracks
    # UNTRUSTED_CONTEXT_HEADER's live length via fence_untrusted_text (SEC-28(B)
    # gap-closure hardening, 18-06). A hand-rolled local overhead constant here
    # would go stale every time the header text changes (as happened when the
    # header gained its anti-demanded-token clause) -- use the real function
    # instead of a second hand-maintained magic number (Pitfall 2).
    target = TARGET(context_window)

    model_fn, calls = _scripted_model(["Draft one. [doc0]", "Draft two. [doc0]"])
    review_fn = _reject_once_then_approve(requery="more")

    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=_retrieve_growing, review_fn=review_fn,
                     context_window=context_window)

    # draft (round 0) + revise (round 1, captured AFTER grounding.append grew the
    # raw list from 10 to 16 oversized items) — proves curation runs per call,
    # not once (Pitfall 4).
    assert len(calls["prompts"]) == 2
    for prompt in calls["prompts"]:
        assert fits(prompt, target), f"prompt of length {len(prompt)} exceeds bound {target}"
    assert r.approved is True
    assert r.grounded is True


def test_history_distilled_in_revise_and_repair():
    long_draft = "D" * 2000
    long_critique = "C" * 2000

    def retrieve_small(q, k):
        return [{"source": "doc1", "text": "The capital of France is Paris."}]

    def review_fn():
        state = {"n": 0}

        def _fn(question, draft, ctx):
            state["n"] += 1
            if state["n"] == 1:
                return {"approved": False, "critique": long_critique}
            return {"approved": True, "critique": "ok"}

        return _fn

    # Guard ON: draft + critique distilled before entering the revise prompt.
    model_fn, calls = _scripted_model([long_draft, "Final answer. [doc1]"])
    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=retrieve_small, review_fn=review_fn(),
                     context_window=800)
    revise_prompt = calls["prompts"][1]
    assert long_draft not in revise_prompt
    assert long_critique not in revise_prompt
    assert r.approved is True

    # Flag-off contrast (GUARD-04): the SAME long draft/critique pass through
    # UNDISTILLED when context_window is None.
    model_fn_off, calls_off = _scripted_model([long_draft, "Final answer. [doc1]"])
    run_pipeline("What is the capital of France?",
                model_fn=model_fn_off, retrieve_fn=retrieve_small, review_fn=review_fn(),
                context_window=None)
    revise_prompt_off = calls_off["prompts"][1]
    assert long_draft in revise_prompt_off
    assert long_critique in revise_prompt_off
    assert len(revise_prompt) < len(revise_prompt_off)


# --- GUARD-05: assert fits(assembled_prompt, TARGET) at the injected model_fn/
# author_fn seam (NOT src.llm_core -- that seam is never on answer_pipeline's
# call path, per 03-RESEARCH.md's "Correction to CONTEXT.md") for the draft
# and revise prompts (sandbox-repair coverage follows in Task 2). ---
# TARGET/_STATIC_OVERHEAD promoted to src/answer_pipeline.py (Phase 8, 08-01,
# D-08 prerequisite) -- imported above, single source of truth.


def test_assembled_prompt_fits_target_window():
    # A realistic guard window (DEFAULT_WINDOW) with grounding large enough
    # that curate() actually engages (raw grounding > window, both before AND
    # after growth), so a captured prompt staying within TARGET proves
    # curation ran -- not that the inputs happened to already fit.
    context_window = DEFAULT_WINDOW
    target = TARGET(context_window)

    def _retrieve(q, k):
        if q == "more evidence":
            return _oversized_hits("extra", 8, size=500)  # 4000 NEW chars
        return _oversized_hits("doc", 10, size=500)  # 5000 chars > DEFAULT_WINDOW

    review_fn = _reject_once_then_approve(requery="more evidence")
    model_fn, calls = _scripted_model(
        ["Grounded draft one. [doc0]", "Grounded draft two, revised. [doc0]"]
    )

    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=_retrieve, review_fn=review_fn,
                     context_window=context_window)

    # draft (round 0) + revise (round 1, captured AFTER grounding.append grew
    # the raw list with the "extra" batch) -- covers a post-growth round
    # (Pitfall 4), not just round 0.
    assert len(calls["prompts"]) == 2
    for p in calls["prompts"]:
        assert fits(p, target), f"prompt of length {len(p)} exceeds TARGET {target}"
    assert r.approved is True
    assert r.grounded is True


# --- GUARD-05 continued: sandbox-repair prompt coverage + Pitfall-1 grounded-
# gate guard. The ONE legitimate monkeypatch target here is
# src.council.verify.verify_answer (a plain function import inside
# run_pipeline), NOT src.llm_core -- see 03-PATTERNS.md. ---

def test_repair_prompt_fits_target_window(monkeypatch):
    context_window = DEFAULT_WINDOW
    target = TARGET(context_window)

    def retrieve_code(q, k):
        return _oversized_hits("doc", 10, size=500)  # forces curation to engage

    def approve_review(question, draft, ctx):
        return {"approved": True, "critique": "ok"}

    # Draft contains fenced code so the sandbox gate's `has_code` check fires.
    model_fn, calls = _scripted_model([
        "```python\ndef f(:\n    pass\n```",
        "```python\ndef f():\n    return 1\n```",
    ])

    call_state = {"n": 0}

    def fake_verify_answer(text, execute=False):
        call_state["n"] += 1
        if call_state["n"] == 1:
            # SHORT scripted detail (Pitfall 2): `detail` is deliberately
            # unbounded/out-of-scope this phase, so a long detail here would
            # make the fit assertion vacuous or mask the known residual gap.
            return {"passed": False, "verified": False, "has_code": True,
                    "syntax_ok": False, "detail": "SyntaxError: invalid syntax"}
        return {"passed": True, "verified": True, "has_code": True,
                "syntax_ok": True, "detail": ""}

    monkeypatch.setattr("src.council.verify.verify_answer", fake_verify_answer)

    run_pipeline("Write a function that returns 1.",
                model_fn=model_fn, retrieve_fn=retrieve_code, review_fn=approve_review,
                context_window=context_window, code_verify=True)

    # calls["prompts"]: [0] initial draft, [1] repair (the scripted sandbox
    # failure forced a repair round to actually build/capture the repair
    # prompt at the injected model_fn seam).
    assert len(calls["prompts"]) == 2
    repair_prompt = calls["prompts"][1]
    assert fits(repair_prompt, target), (
        f"repair prompt of length {len(repair_prompt)} exceeds TARGET {target}")
    # Pitfall 1 / T-5-02 canary: proves the scripted stub (not a real subprocess
    # executor) was consulted for BOTH the initial check and the repair recheck
    # via the still-working monkeypatch("src.council.verify.verify_answer", ...)
    # target -- i.e. verified_gate reaches the gate by qualified module
    # reference, not a module-level name bind. A future rebind of verify_answer
    # at module scope would fail this loudly instead of passing green-for-the-
    # wrong-reason.
    assert call_state["n"] == 2


def test_answer_gate_uses_shared_core(monkeypatch):
    # Spy proving the Answer path actually INVOKES src.verified_gate.evaluate
    # (not merely imports it) -- UNIFY-03 "used, not just imported".
    context_window = DEFAULT_WINDOW

    def retrieve_code(q, k):
        return _oversized_hits("doc", 10, size=500)  # forces curation to engage

    def approve_review(question, draft, ctx):
        return {"approved": True, "critique": "ok"}

    # Draft contains fenced code so the sandbox gate's has_code check fires;
    # scripted bad-then-good pattern (fails initial verify, passes repair).
    model_fn, calls = _scripted_model([
        "```python\ndef f(:\n    pass\n```",
        "```python\ndef f():\n    return 1\n```",
    ])

    call_state = {"n": 0}

    def fake_verify_answer(text, execute=False):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return {"passed": False, "verified": False, "has_code": True,
                    "syntax_ok": False, "detail": "SyntaxError: invalid syntax"}
        # Genuinely-checked clean python -- verified=True (Plan 11-01/11-04:
        # code_verified is now sourced from "verified", not "passed").
        return {"passed": True, "verified": True, "has_code": True,
                "syntax_ok": True, "detail": ""}

    # Scripted stub so no real subprocess runs (same seam the plan's other
    # sandbox tests use).
    monkeypatch.setattr("src.council.verify.verify_answer", fake_verify_answer)

    spy_calls = {"n": 0}
    real_evaluate = verified_gate.evaluate

    def spy_evaluate(candidate, *, execute=False):
        spy_calls["n"] += 1
        return real_evaluate(candidate, execute=execute)

    monkeypatch.setattr(verified_gate, "evaluate", spy_evaluate)

    r = run_pipeline("Write a function that returns 1.",
                     model_fn=model_fn, retrieve_fn=retrieve_code, review_fn=approve_review,
                     context_window=context_window, code_verify=True)

    # Initial check + repair recheck == 2 genuine calls into the shared core.
    assert spy_calls["n"] >= 2
    assert r.approved is True


def test_curation_does_not_defeat_grounded_gate():
    # Pitfall 1: with a REASONABLE context_window (several _MIN_PER_ITEM
    # multiples, e.g. DEFAULT_WINDOW) and non-empty mandatory retrieval, at
    # least one grounding item must survive curation so the model actually
    # sees real grounding text -- proving an aggressive-but-reasonable window
    # does not silently defeat the raw grounded gate.
    context_window = DEFAULT_WINDOW

    def retrieve_reasonable(q, k):
        return [{"source": "doc1", "text": "The capital of France is Paris, a city on the Seine."}]

    def approve_review(question, draft, ctx):
        return {"approved": True, "critique": "ok"}

    model_fn, calls = _scripted_model(["Paris is the capital. [doc1]"])

    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=retrieve_reasonable, review_fn=approve_review,
                     context_window=context_window)

    draft_prompt = calls["prompts"][0]
    assert "(no grounding retrieved)" not in draft_prompt
    assert "doc1" in draft_prompt
    assert r.grounded is True


# --- Phase 8, 08-03: default-on flip (FRESH-01), context_fit-vs-TARGET signal
# (FRESH-02), bounded sandbox-repair detail (D-11), and the bounded/optional
# AnswerRequest.context_window field (V5). These four tests are RED until
# Task 2/3 land the flip + signal + distill. ---


def test_default_on_curates_grounding_without_kwarg():
    # No context_window kwarg at all -- proves run_pipeline's own DEFAULT
    # curates (D-02), not just an explicitly-passed window. curate()'s drop
    # decision is item-count-vs-max_items (DEFAULT_WINDOW // _MIN_PER_ITEM ==
    # 4000 // 240 == 16), so the grounding must GROW PAST 16 live items (not
    # merely exceed DEFAULT_WINDOW in raw chars) for a `budget` (drop) entry
    # to fire -- 12 initial + 10 more (growing to 22) forces an actual drop
    # at the new default window.
    def _retrieve_growing_past_max_items(q, k):
        if q == "more":
            return _oversized_hits("extra", 10)
        return _oversized_hits("doc", 12)

    model_fn, calls = _scripted_model(["Draft one. [doc0]", "Draft two. [doc0]"])
    review_fn = _reject_once_then_approve(requery="more")

    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=_retrieve_growing_past_max_items,
                     review_fn=review_fn)

    assert len(calls["prompts"]) == 2
    budget_entries = [t for t in r.transcript if t.get("stage") == "budget"]
    assert budget_entries, (
        "expected a budget transcript entry proving curation ran at the "
        "default window when context_window is omitted entirely")
    assert r.approved is True


def test_context_fit_entries_assert_against_target(monkeypatch):
    # A single scenario covering draft, revise (post-growth), AND sandbox-repair
    # -- each in-scope model_fn/author_fn call must emit a context_fit
    # transcript entry asserted against TARGET(context_window) (D-05/06/08).
    context_window = DEFAULT_WINDOW
    target = TARGET(context_window)

    def _retrieve(q, k):
        if q == "more evidence":
            return _oversized_hits("extra", 8, size=500)  # growth on requery
        return _oversized_hits("doc", 10, size=500)  # > DEFAULT_WINDOW

    review_fn = _reject_once_then_approve(requery="more evidence")

    # [0] initial draft (no code, rejected by review_fn) -> [1] revise (bad
    # code, approved by review_fn but fails the sandbox check) -> [2] repair
    # (good code, passes the sandbox recheck).
    model_fn, calls = _scripted_model([
        "Grounded draft one. [doc0]",
        "```python\ndef f(:\n    pass\n```",
        "```python\ndef f():\n    return 1\n```",
    ])

    call_state = {"n": 0}

    def fake_verify_answer(text, execute=False):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return {"passed": False, "verified": False, "has_code": True,
                    "syntax_ok": False, "detail": "SyntaxError: invalid syntax"}
        # Genuinely-checked clean python -- verified=True (Plan 11-01/11-04:
        # code_verified is now sourced from "verified", not "passed").
        return {"passed": True, "verified": True, "has_code": True,
                "syntax_ok": True, "detail": ""}

    monkeypatch.setattr("src.council.verify.verify_answer", fake_verify_answer)

    r = run_pipeline("Write a function that returns 1.",
                     model_fn=model_fn, retrieve_fn=_retrieve, review_fn=review_fn,
                     context_window=context_window, code_verify=True)

    # Three model_fn calls -> draft, revise, repair -- confirms all three
    # in-scope call sites actually fired this scenario, not fewer.
    assert len(calls["prompts"]) == 3

    fit_entries = [t for t in r.transcript if t.get("stage") == "context_fit"]
    assert len(fit_entries) == 3, (
        f"expected 3 context_fit entries (draft/revise/repair), got {len(fit_entries)}: "
        f"{fit_entries}")
    for entry in fit_entries:
        assert entry["fits"] is True
        assert isinstance(entry["size"], int)
        assert entry["size"] <= target
        assert entry["window"] == context_window


def test_bounded_detail_excludes_oversized_verbatim_text(monkeypatch):
    # D-11: the sandbox verify `detail` must be distilled before it enters the
    # repair prompt. Script an oversized detail (~3000 chars) on the initial
    # (failing) verify call and assert it does NOT appear verbatim in the
    # captured repair prompt.
    context_window = DEFAULT_WINDOW

    def retrieve_code(q, k):
        return _oversized_hits("doc", 10, size=500)  # forces curation to engage

    def approve_review(question, draft, ctx):
        return {"approved": True, "critique": "ok"}

    model_fn, calls = _scripted_model([
        "```python\ndef f(:\n    pass\n```",
        "```python\ndef f():\n    return 1\n```",
    ])

    oversized_detail = "E" * 3000

    call_state = {"n": 0}

    def fake_verify_answer(text, execute=False):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return {"passed": False, "verified": False, "has_code": True,
                    "syntax_ok": False, "detail": oversized_detail}
        return {"passed": True, "verified": True, "has_code": True,
                "syntax_ok": True, "detail": ""}

    monkeypatch.setattr("src.council.verify.verify_answer", fake_verify_answer)

    run_pipeline("Write a function that returns 1.",
                model_fn=model_fn, retrieve_fn=retrieve_code, review_fn=approve_review,
                context_window=context_window, code_verify=True)

    repair_prompt = calls["prompts"][1]
    assert oversized_detail not in repair_prompt, (
        "oversized sandbox detail leaked verbatim into the repair prompt -- "
        "D-11 requires it be distilled first")


# --- Gap closure 08-05 (WR-01/WR-02/IN-01/IN-02): the context_fit signal must
# be an HONEST, INTERNALLY CONSISTENT, MEANINGFUL within-budget proof — the
# published `budget` must be the exact value `fits` is compared against, the
# budget must account for the ACTUAL question/grounding-formatting overhead
# (not a fixed short-test-question assumption), and the signal must be able to
# report both True and False for real inputs. These four tests are RED until
# Task 2 lands the honest budget + PLAN-call coverage. ---


def test_context_fit_budget_is_internally_consistent(monkeypatch):
    context_window = DEFAULT_WINDOW

    def _retrieve(q, k):
        if q == "more evidence":
            return _oversized_hits("extra", 8, size=500)  # growth on requery
        return _oversized_hits("doc", 10, size=500)  # > DEFAULT_WINDOW

    review_fn = _reject_once_then_approve(requery="more evidence")

    # [0] initial draft (rejected) -> [1] revise (bad code, fails sandbox) ->
    # [2] repair (good code, passes sandbox recheck) -- covers draft/revise/
    # repair in one scenario, mirroring test_context_fit_entries_assert_against_target.
    model_fn, calls = _scripted_model([
        "Grounded draft one. [doc0]",
        "```python\ndef f(:\n    pass\n```",
        "```python\ndef f():\n    return 1\n```",
    ])

    call_state = {"n": 0}

    def fake_verify_answer(text, execute=False):
        call_state["n"] += 1
        if call_state["n"] == 1:
            return {"passed": False, "verified": False, "has_code": True,
                    "syntax_ok": False, "detail": "SyntaxError: invalid syntax"}
        # Genuinely-checked clean python -- verified=True (Plan 11-01/11-04:
        # code_verified is now sourced from "verified", not "passed").
        return {"passed": True, "verified": True, "has_code": True,
                "syntax_ok": True, "detail": ""}

    monkeypatch.setattr("src.council.verify.verify_answer", fake_verify_answer)

    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=_retrieve, review_fn=review_fn,
                     context_window=context_window, code_verify=True)

    fit_entries = [t for t in r.transcript if t.get("stage") == "context_fit"]
    assert fit_entries, "expected at least one context_fit entry"
    for entry in fit_entries:
        assert isinstance(entry["budget"], int), (
            f"context_fit entry missing an int 'budget' key: {entry}")
        assert entry["fits"] == (entry["size"] <= entry["budget"]), (
            f"fits must equal size <= budget: {entry}")
        assert entry["window"] == context_window


def test_context_fit_false_for_oversized_question():
    context_window = DEFAULT_WINDOW
    # The AnswerRequest field maximum (V5 bound, routes/answer_routes.py) --
    # a genuinely pathological question, far larger than the fresh window.
    oversized_question = "Q" * 20000

    def retrieve_reasonable(q, k):
        return [{"source": "doc1", "text": "The capital of France is Paris, a city on the Seine."}]

    def approve_review(question, draft, ctx):
        return {"approved": True, "critique": "ok"}

    model_fn, calls = _scripted_model(["Paris is the capital. [doc1]"])

    r = run_pipeline(oversized_question,
                     model_fn=model_fn, retrieve_fn=retrieve_reasonable, review_fn=approve_review,
                     context_window=context_window)

    fit_entries = [t for t in r.transcript if t.get("stage") == "context_fit"]
    assert fit_entries, "expected at least one context_fit entry"
    false_entries = [e for e in fit_entries if e["fits"] is False]
    assert false_entries, (
        "an oversized (20000-char) question should make at least one "
        f"context_fit entry report fits=False: {fit_entries}")
    for e in false_entries:
        assert e["size"] > e["budget"]


def test_context_fit_true_for_realistic_long_question():
    # A tighter window (not DEFAULT_WINDOW) so a realistic long question's
    # actual weight relative to the window is pronounced enough to expose the
    # old fixed-STATIC_OVERHEAD undercount (WR-01): with curated grounding
    # ALSO engaged, the old TARGET(context_window) (no question/grounding-fmt
    # awareness) reports a spurious fits=False here even though the pipeline
    # is behaving correctly -- the honest budget must report True.
    context_window = 1200

    # Realistic long question: ~1300 chars -- legitimately long, but nowhere
    # near the 20000-char API maximum -- must NOT produce a spurious False.
    realistic_question = (
        "Please explain in detail how the French government is structured, "
        * 20
    ).strip()
    assert 800 <= len(realistic_question) <= 1500

    def retrieve_reasonable(q, k):
        return _oversized_hits("doc", 10, size=300)  # forces curation to engage

    def approve_review(question, draft, ctx):
        return {"approved": True, "critique": "ok"}

    model_fn, calls = _scripted_model(["Paris is the capital. [doc0]"])

    r = run_pipeline(realistic_question,
                     model_fn=model_fn, retrieve_fn=retrieve_reasonable, review_fn=approve_review,
                     context_window=context_window)

    fit_entries = [t for t in r.transcript if t.get("stage") == "context_fit"]
    assert fit_entries, "expected at least one context_fit entry"
    for e in fit_entries:
        assert isinstance(e["budget"], int), (
            f"context_fit entry missing an int 'budget' key: {e}")
        assert e["fits"] is True, (
            f"realistic long question ({len(realistic_question)} chars) with "
            f"curated grounding should still fit (no spurious False): {e}")


def test_context_fit_covers_structured_plan_call():
    context_window = DEFAULT_WINDOW

    def retrieve_reasonable(q, k):
        return [{"source": "doc1", "text": "The capital of France is Paris, a city on the Seine."}]

    def approve_review(question, draft, ctx):
        return {"approved": True, "critique": "ok"}

    def plan_fn(prompt):
        return "1. Identify the capital.\n2. Cite the source.\n3. Answer."

    model_fn, calls = _scripted_model(["Paris is the capital. [doc1]"])

    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=retrieve_reasonable, review_fn=approve_review,
                     context_window=context_window, structured=True, plan_fn=plan_fn)

    fit_entries = [t for t in r.transcript if t.get("stage") == "context_fit"]
    # draft + plan, at minimum -- proves the PLAN call is now fit-covered (IN-02).
    assert len(fit_entries) >= 2, (
        f"expected at least 2 context_fit entries (plan + draft), got "
        f"{len(fit_entries)}: {fit_entries}")
    plan_entries = [e for e in fit_entries if e.get("call") == "plan"]
    assert len(plan_entries) == 1, (
        "expected exactly one context_fit entry tagged for the structured "
        f"PLAN model call (IN-02): {fit_entries}")


def test_context_window_field_bounded_and_optional():
    # V5: AnswerRequest.context_window is a bounded optional field. None/omitted
    # must map to the new default-on behavior (route forwards it conditionally,
    # see Task 2); negative / absurdly large values must be rejected here at
    # the API boundary (T-08-04, DoS mitigation).
    from routes.answer_routes import AnswerRequest

    assert AnswerRequest(prompt="x").context_window is None

    with pytest.raises(ValidationError):
        AnswerRequest(prompt="x", context_window=-1)

    with pytest.raises(ValidationError):
        AnswerRequest(prompt="x", context_window=10**9)

    assert AnswerRequest(prompt="x", context_window=2000).context_window == 2000
