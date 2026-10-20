# tests/test_verified_gate.py
"""Pure unit coverage for src/verified_gate.py's evaluate()/resolve() (UNIFY-01).

No async, no fixtures beyond scripted dicts/monkeypatches — evaluate()/resolve()
are pure, synchronous, never-raises functions (D-01, D-02, D-04, D-06)."""
from __future__ import annotations

import src.verified_gate as vg


def test_evaluate_delegates_to_verify_answer(monkeypatch):
    """evaluate() must reach verify_answer via the qualified module reference
    (src.council.verify.verify_answer) so monkeypatching that attribute
    redirects every call — the Pitfall 1 canary at unit level."""
    calls = []

    def fake_verify_answer(text, execute=False):
        calls.append((text, execute))
        return {"has_code": True, "syntax_ok": True, "ran": True,
                "passed": True, "detail": "", "blocks": 1}

    monkeypatch.setattr("src.council.verify.verify_answer", fake_verify_answer)

    verdict, needs_repair, why = vg.evaluate("candidate code", execute=True)

    assert calls == [("candidate code", True)]
    assert verdict == {"has_code": True, "syntax_ok": True, "ran": True,
                        "passed": True, "detail": "", "blocks": 1}
    assert needs_repair is False
    assert why is None


def test_evaluate_why_did_not_parse(monkeypatch):
    monkeypatch.setattr(
        "src.council.verify.verify_answer",
        lambda text, execute=False: {"passed": False, "syntax_ok": False},
    )
    verdict, needs_repair, why = vg.evaluate("bad code")
    assert needs_repair is True
    assert why == "did not parse"


def test_evaluate_why_failed_when_run(monkeypatch):
    monkeypatch.setattr(
        "src.council.verify.verify_answer",
        lambda text, execute=False: {"passed": False, "syntax_ok": True},
    )
    verdict, needs_repair, why = vg.evaluate("bad code", execute=True)
    assert needs_repair is True
    assert why == "failed when run"


def test_evaluate_passed_has_no_repair_needed(monkeypatch):
    monkeypatch.setattr(
        "src.council.verify.verify_answer",
        lambda text, execute=False: {"passed": True, "syntax_ok": True},
    )
    verdict, needs_repair, why = vg.evaluate("good code")
    assert needs_repair is False
    assert why is None


def test_resolve_accepted_when_repair_passed():
    accepted, final_text, final_verdict = vg.resolve(
        {"passed": False}, "fixed code", {"passed": True},
    )
    assert accepted is True
    assert final_text == "fixed code"
    assert final_verdict == {"passed": True}


def test_resolve_accepted_when_initial_failed_even_if_repair_also_failed():
    # D-06: accept non-empty repair output whenever the initial verdict failed,
    # even if the repair verdict also fails -- something differs from before.
    accepted, final_text, final_verdict = vg.resolve(
        {"passed": False}, "still broken but different", {"passed": False},
    )
    assert accepted is True
    assert final_text == "still broken but different"
    assert final_verdict == {"passed": False}


def test_resolve_rejected_when_initial_passed_and_repair_failed():
    accepted, final_text, final_verdict = vg.resolve(
        {"passed": True}, "unnecessary repair", {"passed": False},
    )
    assert accepted is False
    assert final_text is None
    assert final_verdict is None


def test_resolve_rejected_on_empty_repaired_text():
    accepted, final_text, final_verdict = vg.resolve(
        {"passed": False}, "", {"passed": True},
    )
    assert accepted is False


def test_resolve_rejected_on_whitespace_only_repaired_text():
    accepted, final_text, final_verdict = vg.resolve(
        {"passed": False}, "   \n\t  ", {"passed": True},
    )
    assert accepted is False


def test_resolve_rejected_on_none_repaired_text_never_raises():
    """Pitfall 3: Answer's repair text can be None; resolve() must not raise
    (the `(repaired_text or "").strip()` guard) and must reject."""
    accepted, final_text, final_verdict = vg.resolve(
        {"passed": True}, None, {"passed": True},
    )
    assert accepted is False
    assert final_text is None
    assert final_verdict is None


def test_evaluate_and_resolve_are_pure_and_never_raise(monkeypatch):
    """Pitfall 4: malformed/empty verdict dicts (missing keys) must never raise
    a KeyError/AttributeError through .get()-based access, in either evaluate()
    or resolve()."""
    # evaluate() with a verdict missing every expected key.
    monkeypatch.setattr("src.council.verify.verify_answer", lambda text, execute=False: {})
    verdict, needs_repair, why = vg.evaluate("anything")
    assert verdict == {}
    assert needs_repair is True
    assert why == "did not parse"

    # resolve() with fully malformed/empty dicts on both sides.
    result = vg.resolve({}, "x", {})
    assert isinstance(result, tuple) and len(result) == 3

    # resolve() with malformed dicts and falsy repaired text.
    result2 = vg.resolve({}, "", {})
    assert result2 == (False, None, None)

    result3 = vg.resolve({}, None, {})
    assert result3 == (False, None, None)
