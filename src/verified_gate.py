# src/verified_gate.py
"""Neutral, path-agnostic verify-and-repair decision core (UNIFY-01, UNIFY-03).

Pure, synchronous, NEVER RAISES. Owns exactly the verdict/accept judgment both
Council (`orchestrator.run_council`) and Answer (`answer_pipeline.run_pipeline`)
already run identically today. Callers keep their own generation transport
(async streaming vs sync calls) and their own post-effects (event emission,
approval demotion, repair-prompt text) -- see 05-CONTEXT.md D-01/D-06/D-07.

`src/council/verify.py` stays the internal dependency (D-04); imported by
MODULE reference (not by name) so `monkeypatch.setattr(
"src.council.verify.verify_answer", ...)` in existing tests keeps working
regardless of when this module itself is imported (05-RESEARCH.md Pitfall 1).
"""
from __future__ import annotations

from src.council import verify as _verify


def evaluate(candidate: str, *, execute: bool = False) -> tuple[dict, bool, str | None]:
    """Run the verify gate once. Returns (verdict, needs_repair, why).

    `why` is None when needs_repair is False (nothing to explain). Callers
    reuse this for BOTH the initial check and the repair round's recheck --
    on the recheck, needs_repair/why are simply unused (no third round)."""
    verdict = _verify.verify_answer(candidate, execute=execute)
    needs_repair = not verdict.get("passed")
    why = None
    if needs_repair:
        why = "did not parse" if not verdict.get("syntax_ok") else "failed when run"
    return verdict, needs_repair, why


def resolve(initial_verdict: dict, repaired_text: str,
           repair_verdict: dict) -> tuple[bool, str | None, dict | None]:
    """Accept-or-keep the repaired candidate against the shared condition both
    paths already use: `fixed.strip() and (repair_verdict.passed or not
    initial_verdict.passed)` (D-06). Returns (accepted, final_text, final_verdict).

    On accepted=False, final_text/final_verdict are None -- this function is
    never given the pre-repair text (only the pre-repair VERDICT), so it
    cannot itself supply a fallback; callers already leave their own
    draft/final_content variable untouched on reject today, so None here is
    an explicit "caller: keep your own state" signal, not an omission."""
    accepted = bool((repaired_text or "").strip()) and (
        bool(repair_verdict.get("passed")) or not bool(initial_verdict.get("passed"))
    )
    if not accepted:
        return False, None, None
    return True, repaired_text, repair_verdict
