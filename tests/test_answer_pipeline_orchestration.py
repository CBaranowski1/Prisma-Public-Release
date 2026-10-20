"""Offline, stub-driven tests for the answer_pipeline orchestration layer:
self-consistency (samples>1) and plan->execute->verify (structured=True).

No real LLM / brain — retrieve_fn/model_fn/review_fn are fakes. Proves the new
opt-in stages behave, and that defaults reproduce the pre-change single-pass path.
"""
from __future__ import annotations

from src.answer_pipeline import run_pipeline, _vote


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


def test_self_consistency_picks_majority():
    # Two identical ("Paris.") + one different ("London.") -> consensus = Paris.
    model_fn, calls = _scripted_model(["Paris.", "London.", "Paris."])
    r = run_pipeline("What is the capital of France?",
                     model_fn=model_fn, retrieve_fn=_retrieve, review_fn=_approve_review,
                     samples=3, context_window=None)
    assert calls["n"] == 3
    assert r.answer == "Paris."
    assert r.orchestration["samples"] == 3
    assert r.orchestration["consensus_cluster_size"] == 2
    assert r.approved is True


def test_structured_runs_plan_and_verify():
    # scripted[0] -> PLAN (plan_fn defaults to model_fn), scripted[1] -> draft.
    model_fn, calls = _scripted_model(
        ["1. Recall grounding\n2. Answer\n3. Cite", "Paris. [doc1]"])
    seen = {"verify": 0}

    def verify_fn(question, answer):
        seen["verify"] += 1
        return {"approved": True, "critique": "meets all constraints"}

    r = run_pipeline("What is the capital of France? Cite the source.",
                     model_fn=model_fn, retrieve_fn=_retrieve, review_fn=_approve_review,
                     structured=True, verify_fn=verify_fn, context_window=None)
    roles = [t["role"] for t in r.transcript]
    assert "plan" in roles and "verify" in roles
    assert seen["verify"] == 1              # final gate, called exactly once
    assert r.orchestration["structured"] is True
    assert r.orchestration["verified"] is True
    assert "PLAN:" in calls["prompts"][1]   # plan scaffolds the draft prompt


def test_structured_verify_can_demote_approval():
    model_fn, _ = _scripted_model(["1. plan", "Guess: maybe Lyon"])

    def verify_fail(question, answer):
        return {"approved": False, "critique": "does not cite the source as required"}

    r = run_pipeline("Capital of France? Cite the source.",
                     model_fn=model_fn, retrieve_fn=_retrieve, review_fn=_approve_review,
                     structured=True, verify_fn=verify_fail, context_window=None)
    assert r.approved is False              # failed final VERIFY demotes approval
    assert r.orchestration["verified"] is False


def test_defaults_are_single_pass_unchanged():
    model_fn, calls = _scripted_model(["Paris. [doc1]"])
    r = run_pipeline("Capital of France?",
                     model_fn=model_fn, retrieve_fn=_retrieve, review_fn=_approve_review,
                     context_window=None)
    assert calls["n"] == 1                  # exactly one draft call
    assert r.answer == "Paris. [doc1]"
    assert r.approved is True
    roles = {t["role"] for t in r.transcript}
    assert "plan" not in roles and "verify" not in roles
    assert r.orchestration == {"samples": 1, "structured": False}


def test_vote_text_and_code_clustering():
    rep, size = _vote(["Paris.", "London.", "Paris."])
    assert rep == "Paris." and size == 2
    # AST-normalized code: reindented / re-spaced variants cluster together.
    a = "```python\ndef f(x):\n    return x+1\n```"
    b = "```python\ndef f(x):\n        return x + 1\n```"
    c = "```python\ndef g():\n    return 0\n```"
    _, size2 = _vote([a, b, c])
    assert size2 == 2
