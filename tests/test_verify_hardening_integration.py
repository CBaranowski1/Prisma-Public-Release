"""SC-5 / D-06 live-proof — a single integration-checker connection driving
ONE real `run_council(...)` that proves Phase 11's three recall+verify
hardening seams are genuinely WIRED on the exact path Phase 12's A/B will
measure, not merely present as green unit tests ("presence != integration",
the milestone's core principle):

  1. The widened verify gate (`src.council.verify.verify_answer`, Plan
     11-01/11-04) actually runs against the synthesizer's final answer.
  2. The recalled brain note AND the injected skill body are fenced as
     UNTRUSTED via `untrusted_context_message` (Plan 11-02/SEC-10) — not
     merely present in the assembled messages.
  3. The embedder-identity check (`src.embeddings._embedder_identity`, Plan
     11-03/GRAY-BRN-01) fires on the genuine recall path (a real
     fastembed-backed `BrainIndex`, not the index-less lexical fixture used
     elsewhere in this repo) — proving GRAY-BRN-01's fail-loud rail is live,
     not merely unit-tested against a hand-built fixture.

Every spy below patches a LIVE module/instance attribute the assembled
system actually resolves at call time
(`monkeypatch.setattr(module_or_instance, name, MagicMock(wraps=...))`) and
asserts genuine invocation via call-count — never a string-path patch, never
a "symbol is present" smoke assertion, and never a direct call to the spied
capability from this test body (Phase 9/10 convention, D-06).

Two seam-specific LIVE-ATTRIBUTE traps this file deliberately avoids:

  - `src/council/seat_runner.py` imports `untrusted_context_message` via
    `from src.prompt_security import untrusted_context_message` — a NAME
    BINDING into seat_runner's own module namespace. The attribute
    `build_messages_with_brain` actually resolves at call time is
    `seat_runner.untrusted_context_message`, NOT
    `src.prompt_security.untrusted_context_message` — patching the latter
    would silently never fire (mirrors the `providers/precision_vault.py`
    dual-module-identity trap documented in STATE.md Phase 9 P5).
  - `src/brain_index.py::BrainIndex._check_embedder_identity` does a LAZY
    `from src.embeddings import get_embedding_client, _embedder_identity`
    INSIDE the method body, executed fresh on every call — so patching the
    module attribute `src.embeddings._embedder_identity` genuinely IS visible
    the next time that lazy import runs, unlike a module-top import (which
    would have bound a stale reference before the patch ever existed).
  - By contrast, `src/verified_gate.py` imports verify by MODULE reference
    (`from src.council import verify as _verify`, its own docstring says so
    explicitly) precisely so a `monkeypatch.setattr(
    "src.council.verify.verify_answer", ...)`-style live-attribute patch
    keeps working regardless of import order — patching
    `src.council.verify.verify_answer` is therefore the correct target.

Council harness (`_make_stubs`/`_run`) copied — not imported — from
tests/test_tool_through_team.py / tests/test_unified_pipeline_e2e.py; the
fixture brain-note vault text copied from tests/test_council_note_recall.py
(repo's copy-don't-import convention). UNLIKE those two files' index-less
(`index_dir=None`) fixture registries, this file builds a REAL fastembed
index via `src.brain_index.build_index` so recall takes the vector-index
fast path through `BrainIndex.search()` -> `_ensure_main()` ->
`_check_embedder_identity()` — the exact path GRAY-BRN-01's identity check
lives on. fastembed 0.8.0 is present in this dev environment; the whole
module skips visibly (never silently) if it's ever absent.

Offline throughout: no live network, no live Chroma, no `import app`.
"""
from __future__ import annotations

import asyncio
import importlib.util
import pathlib
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastembed", reason="GRAY-BRN-01 identity check requires a real "
                                         "fastembed-backed BrainIndex; pip install fastembed")

import src.brain_index as bi
import src.council.verify as verify_mod
import src.embeddings as embeddings_mod
from src.council import seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat
from src.memory_provider import MemoryProviderRegistry

PROMPT = "What are the best AI security guardrails and red teaming tools for our stack?"

_SECURITY_NOTE = """---
title: Awesome AI Security — curated frameworks, standards & tools
tags: [moc, knowledge/security, reference, ai-security]
---

A curated list of awesome AI security frameworks, guardrails, threat models,
red teaming tooling, and prompt-injection defenses for agentic AI systems.
"""


def _cfg() -> CouncilConfig:
    """One panelist seat that owns a skill (so the skill-guidance untrusted
    wrap fires) consumed by a plain synthesizer (so the verify gate has a
    final answer to inspect) — mirrors test_tool_through_team.py's owner-seat
    /consumer shape, minus the tool-dispatch surface (out of this plan's
    scope; SC-5's tool-loop proof already lives in test_tool_through_team.py)."""
    return CouncilConfig(seats=[
        Seat(id="analyst", role="panelist", skills=["code-review"], consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["analyst"]),
    ])


# ---------------------------------------------------------------------------
# Council harness (copied from tests/test_tool_through_team.py /
# tests/test_unified_pipeline_e2e.py, copy-don't-import convention)
# ---------------------------------------------------------------------------

def _make_stubs(monkeypatch, scripted: dict[str, list[str]] | None = None,
                default: str = "canned answer, short and simple."):
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
        yield f"data: {__import__('json').dumps({'delta': content})}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(seat_runner, "_resolve", fake_resolve)
    import src.llm_core as llm_core_mod
    monkeypatch.setattr(llm_core_mod, "llm_call_async", fake_llm_call_async)
    monkeypatch.setattr(llm_core_mod, "stream_llm", fake_stream_llm)
    return captured


async def _run(cfg: CouncilConfig, prompt: str = PROMPT, **kw) -> list[dict]:
    events = []
    async for ev in run_council(prompt, cfg, **kw):
        events.append(ev)
    return events


def _scripted_answers() -> dict[str, list[str]]:
    return {
        "analyst": ["Security posture looks reasonable; consider deeper red-teaming coverage."],
        # A single clean fenced Python block so verify_answer has something
        # genuine to validate (verified=True, no repair round triggered).
        "chief": ["Here is a minimal example:\n\n```python\nprint('hello world')\n```\n"],
    }


# ---------------------------------------------------------------------------
# REAL fastembed-backed brain registry (not the index-less lexical fixture
# used by tests/test_council_note_recall.py / tests/test_tool_through_team.py)
# so recall genuinely takes the BrainIndex vector-search path that
# GRAY-BRN-01's identity check lives on.
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location(
    "precision_vault", pathlib.Path("providers/precision_vault.py"))
_pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pv)


def _fixture_registry_with_real_index(tmp_path: pathlib.Path):
    vault = tmp_path / "vault"
    security_dir = vault / "knowledge" / "security"
    security_dir.mkdir(parents=True)
    (security_dir / "awesome-ai-security.md").write_text(_SECURITY_NOTE, encoding="utf-8")

    brain_dir = tmp_path / "brain"
    # Build the index BEFORE any spy is installed -- this call is test setup,
    # not part of the "through the team" proof, and must not inflate the
    # identity spy's call_count.
    bi.build_index(vault, brain_dir / "index")

    # PrecisionVaultProvider._vector_index() resolves BrainIndex at
    # `Path(index_dir) / "index"`, so index_dir is the BRAIN dir, not the
    # index dir itself.
    provider = _pv.PrecisionVaultProvider(vault_path=str(vault), index_dir=str(brain_dir))
    return MemoryProviderRegistry(providers=[provider])


def test_sc5_verify_untrusted_and_identity_fire_through_the_team(monkeypatch, tmp_path):
    """SC-5 / D-06: drive ONE real run_council(...) and prove, via
    module-object spies on the LIVE attributes the assembled system resolves,
    that the widened verify gate, the untrusted-context fence, and the
    embedder-identity check all fire as side effects — never called directly
    below."""
    registry = _fixture_registry_with_real_index(tmp_path)
    _make_stubs(monkeypatch, scripted=_scripted_answers())

    # Seam 1 (SEC-08, Plan 11-01/11-04): src/verified_gate.py's `evaluate()`
    # calls `_verify.verify_answer(...)` via a MODULE reference
    # (`from src.council import verify as _verify`) -- patching the LIVE
    # `src.council.verify.verify_answer` attribute is visible to that
    # reference regardless of import order.
    verify_spy = MagicMock(wraps=verify_mod.verify_answer)
    monkeypatch.setattr(verify_mod, "verify_answer", verify_spy)

    # Seam 2 (SEC-10, Plan 11-02): the LIVE attribute build_messages_with_
    # brain resolves at call time is seat_runner's OWN name binding (`from
    # src.prompt_security import untrusted_context_message`), not
    # src.prompt_security's module attribute -- see module docstring.
    untrusted_spy = MagicMock(wraps=seat_runner.untrusted_context_message)
    monkeypatch.setattr(seat_runner, "untrusted_context_message", untrusted_spy)

    # Seam 3 (GRAY-BRN-01, Plan 11-03): BrainIndex._check_embedder_identity
    # does a LAZY `from src.embeddings import ..., _embedder_identity` inside
    # its own method body, executed fresh on every call -- patching the
    # module attribute is visible the next time that lazy import runs.
    identity_spy = MagicMock(wraps=embeddings_mod._embedder_identity)
    monkeypatch.setattr(embeddings_mod, "_embedder_identity", identity_spy)

    events = asyncio.run(_run(_cfg(), PROMPT, owner="alice", registry=registry))

    assert events  # sanity: the run produced events

    # Seam 1: the widened verify gate genuinely ran against the final answer.
    assert verify_spy.call_count > 0
    done = next(e for e in events if e["type"] == "done")
    assert done["verified"] is True  # clean fenced python -> honestly verified

    # Seam 2: BOTH the recalled brain note AND the injected skill body were
    # wrapped as untrusted (not merely present in the assembled messages).
    assert untrusted_spy.call_count > 0
    labels = [c.args[0] for c in untrusted_spy.call_args_list]
    assert "brain notes" in labels
    assert any(label.startswith("skill:") for label in labels)

    # Seam 3: the embedder-identity check fired on the genuine recall path.
    assert identity_spy.call_count > 0
