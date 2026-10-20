"""Phase 12 (Effectiveness Measurement A/B) — Wave-0 FAILING contract for
`scripts/ab_harness.py` (12-03/12-04) and `scripts/screen_borderline.py`
(12-02), plus the mocked-council harness fixtures those later waves reuse.

This file is the "harness contract" the plan's RESEARCH.md and VALIDATION.md
describe: symbol names/signatures downstream executors implement against,
encoded here as tests so drift is caught mechanically instead of by a
scavenger hunt through prose. Every harness/screener symbol below (`scripts.
ab_harness.*`, `scripts.screen_borderline.*`) does NOT exist yet as of this
plan (12-01) — each contract test therefore fails RED, at test-body runtime,
for a missing-implementation reason (ModuleNotFoundError/AttributeError) —
never a collection/syntax error. `pytest tests/test_ab_harness_integration.py
-q --co` must exit 0 regardless of which later wave has landed.

CRITICAL collection-safety rule (deviates from the Phase 11 file this harness
is copied from): the not-yet-existing `scripts.ab_harness` / `scripts.
screen_borderline` symbols are imported LAZILY, inside each test body via the
`_h()` / `_screener()` helpers below — NEVER at module top. A module-top
import would make the whole file fail to collect the instant one contract
symbol is still missing, breaking `pytest -k "<selector>"` sampling for every
OTHER already-implemented selector.

Selectors this file must expose (12-VALIDATION.md, Per-Task Verification Map):
    -k "precondition" | "spygate" | "manifest" | "isolation" | "band"

Council harness (`_make_stubs`/`_cfg`/`_run`/`_fixture_registry_with_real_
index`) COPIED — not imported — from `tests/test_verify_hardening_
integration.py` (repo's copy-don't-import convention); that file's own
docstring documents the two name-binding spy traps this file also avoids:

  - `src/council/seat_runner.py` imports `untrusted_context_message` via
    `from src.prompt_security import untrusted_context_message` — a NAME
    BINDING into seat_runner's own module namespace. Patch
    `seat_runner.untrusted_context_message`, never
    `src.prompt_security.untrusted_context_message` (silent no-op).
  - `src/council/orchestrator.py` imports `run_seat`/`stream_seat` via
    `from src.council.seat_runner import run_seat, stream_seat, _build_
    messages` — a SECOND, distinct name-binding trap (RESEARCH.md Pitfall 5b):
    a spy on `seat_runner.run_seat` would never be visible to orchestrator's
    own call sites. "council_seats_ran" is therefore derived from the EVENT
    STREAM (`seat_status` events), never a `run_seat` spy.
  - By contrast, `src/verified_gate.py` imports verify by MODULE reference
    (`from src.council import verify as _verify`), so patching the LIVE
    `src.council.verify.verify_answer` attribute is the correct target.

Fixture brain-note vault text copied from `tests/test_council_note_recall.py`
/ `tests/test_verify_hardening_integration.py`. Offline throughout: no live
network, no live Chroma, no `import app`, no live Ollama (LLM calls mocked
via `_make_stubs`, matching the Phase 9-11 `_make_stubs` convention).
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import pathlib
from types import SimpleNamespace
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

# A minimal HumanEval-shaped problem — enough to drive extract_code/run_tests
# scoring once the real arm exists (12-03), without depending on the actual
# fetched dataset (scripts/fetch_humaneval.py output is not a test fixture).
_PROBLEM = {
    "task_id": "HumanEval/0",
    "prompt": "def add(a, b):\n    \"\"\"Return a + b.\"\"\"\n",
    "test": "def check(candidate):\n    assert candidate(1, 2) == 3\n",
    "entry_point": "add",
    "kind": "function",
}

_SECURITY_NOTE = """---
title: Awesome AI Security — curated frameworks, standards & tools
tags: [moc, knowledge/security, reference, ai-security]
---

A curated list of awesome AI security frameworks, guardrails, threat models,
red teaming tooling, and prompt-injection defenses for agentic AI systems.
"""


# ---------------------------------------------------------------------------
# Harness/screener contract — LAZY imports only (collection-safety rule above)
# ---------------------------------------------------------------------------

def _h():
    """Lazily import the not-yet-existing scripts/ab_harness.py contract
    (12-03/12-04). Raises ModuleNotFoundError until that wave lands — the
    intended Wave-0 RED state."""
    import scripts.ab_harness as h
    return h


def _screener():
    """Lazily import the not-yet-existing scripts/screen_borderline.py
    contract (12-02). Raises ModuleNotFoundError until that wave lands."""
    import scripts.screen_borderline as s
    return s


# ---------------------------------------------------------------------------
# Council harness (copied from tests/test_verify_hardening_integration.py,
# copy-don't-import convention)
# ---------------------------------------------------------------------------

def _cfg() -> CouncilConfig:
    """One panelist seat that owns a skill (so the skill-guidance untrusted
    wrap fires) consumed by a plain synthesizer (so the verify gate has a
    final answer to inspect) — mirrors the Phase 11 minimal-council shape
    (D-01 of this phase's RESEARCH: 1 panelist + 1 synthesizer, not the full
    7-seat DEFAULT_COUNCIL)."""
    return CouncilConfig(seats=[
        Seat(id="analyst", role="panelist", skills=["code-review"], consumes=[]),
        Seat(id="chief", role="synthesizer", consumes=["analyst"]),
    ])


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


_spec = importlib.util.spec_from_file_location(
    "precision_vault", pathlib.Path("providers/precision_vault.py"))
_pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pv)


def _fixture_registry_with_real_index(tmp_path: pathlib.Path):
    """REAL fastembed-backed BrainIndex fixture — not the index-less lexical
    fixture used by other test files — so recall genuinely takes the vector-
    search path the harness's precondition check + GRAY-BRN-01's identity
    check both live on."""
    vault = tmp_path / "vault"
    security_dir = vault / "knowledge" / "security"
    security_dir.mkdir(parents=True)
    (security_dir / "awesome-ai-security.md").write_text(_SECURITY_NOTE, encoding="utf-8")

    brain_dir = tmp_path / "brain"
    bi.build_index(vault, brain_dir / "index")

    provider = _pv.PrecisionVaultProvider(vault_path=str(vault), index_dir=str(brain_dir))
    return MemoryProviderRegistry(providers=[provider])


# ---------------------------------------------------------------------------
# Contract tests — each expected RED until its implementing wave lands.
# ---------------------------------------------------------------------------

def test_precondition_fails_loud_when_fastembed_absent(monkeypatch):
    """MEAS-01 precondition (implemented 12-03-T1): when the harness's own
    fastembed/brain-index probe reports absence, check_preconditions() must
    raise RuntimeError (or return the documented degraded flag) — never
    silently proceed as if recall were full-strength. Expected-RED until
    scripts/ab_harness.py exists (ModuleNotFoundError via _h())."""
    h = _h()
    monkeypatch.setattr(h, "_fastembed_available", lambda: False, raising=False)
    with pytest.raises(RuntimeError):
        h.check_preconditions()


def test_spygate_verify_untrusted_seats_fire_through_team(monkeypatch, tmp_path):
    """MEAS-01 (implemented 12-03-T2): drive ONE real run_council via
    run_real_arm(...) under the mocked-LLM stubs + a REAL fastembed registry;
    prove verify_answer / untrusted_context_message / execute_tool_block all
    fire as side effects (module-object spies, never called directly), that
    council_seats_ran is derived from the seat_status EVENT STREAM (not a
    run_seat spy — Pitfall 5b), and that the honest done["verified"] key is
    read (never done["verification"]["passed"]). Expected-RED until scripts/
    ab_harness.py's run_real_arm/build_harness_council exist."""
    h = _h()
    registry = _fixture_registry_with_real_index(tmp_path)
    _make_stubs(monkeypatch, scripted=_scripted_answers())

    # Seam 1: src/verified_gate.py imports verify by MODULE reference -- safe
    # to patch at origin.
    verify_spy = MagicMock(wraps=verify_mod.verify_answer)
    monkeypatch.setattr(verify_mod, "verify_answer", verify_spy)

    # Seam 2/3: seat_runner's OWN name-bound attributes -- must patch HERE,
    # not src.prompt_security / src.tool_execution.
    untrusted_spy = MagicMock(wraps=seat_runner.untrusted_context_message)
    monkeypatch.setattr(seat_runner, "untrusted_context_message", untrusted_spy)
    tool_spy = MagicMock(wraps=seat_runner.execute_tool_block)
    monkeypatch.setattr(seat_runner, "execute_tool_block", tool_spy)

    cfg = h.build_harness_council()
    events, final = asyncio.run(
        h.run_real_arm(_PROBLEM, registry, cfg, owner="ab-harness"))

    # council_seats_ran via the EVENT STREAM, never a run_seat spy.
    seat_ids = {s.id for s in cfg.active_seats()}
    terminal = {e["seat_id"] for e in events
                if e["type"] == "seat_status" and e["status"] in ("done", "error")}
    assert seat_ids <= terminal

    done = next(e for e in events if e["type"] == "done")
    assert "verified" in done          # honest key, never verification["passed"]
    assert isinstance(done["verified"], bool)
    assert final == done["final"]

    assert verify_spy.call_count > 0
    assert untrusted_spy.call_count > 0
    labels = [c.args[0] for c in untrusted_spy.call_args_list]
    assert any(label.startswith("skill:") for label in labels)


def test_manifest_buckets_and_flags_systemic_noop():
    """MEAS-02 (implemented 12-04-T1): the manifest must bucket
    always-expected keys (council_seats_ran, verify_gate_ran — flagged if
    False on ANY row) separately from conditional keys (brain_recall_hit,
    skills_loaded, query_tool_used — flagged ONLY if False across ALL rows,
    a systemic-no-op signal, not per-problem irrelevance); NO escalation_
    fired key may exist anywhere (Open Question #1, DROPPED per RESEARCH).
    Expected-RED until scripts/ab_harness.py's manifest_from_run/
    flag_systemic_noops exist."""
    h = _h()

    # One row has an always-expected key False -> must flag, regardless of
    # the OTHER row being clean.
    mixed_rows = [
        {"council_seats_ran": True, "verify_gate_ran": True,
         "brain_recall_hit": True, "skills_loaded": True, "query_tool_used": False},
        {"council_seats_ran": False, "verify_gate_ran": True,
         "brain_recall_hit": False, "skills_loaded": True, "query_tool_used": False},
    ]
    flags = h.flag_systemic_noops(mixed_rows)
    assert "council_seats_ran" in flags
    # query_tool_used is False on BOTH rows here -> a genuine systemic no-op.
    assert "query_tool_used" in flags
    # brain_recall_hit is False on only ONE row -> per-problem irrelevance,
    # not systemic -> must NOT be flagged.
    assert "brain_recall_hit" not in flags
    assert not any("escalation" in f for f in flags)

    clean_rows = [
        {"council_seats_ran": True, "verify_gate_ran": True,
         "brain_recall_hit": True, "skills_loaded": True, "query_tool_used": True},
        {"council_seats_ran": True, "verify_gate_ran": True,
         "brain_recall_hit": False, "skills_loaded": True, "query_tool_used": True},
    ]
    flags2 = h.flag_systemic_noops(clean_rows)
    assert "council_seats_ran" not in flags2
    assert "verify_gate_ran" not in flags2
    assert "query_tool_used" not in flags2

    manifest = h.manifest_from_run(events=[], spies={}, before={})
    assert "escalation_fired" not in manifest


def test_flag_systemic_noops_reads_nested_production_row_manifest():
    """Regression (12-05 live-run bug): scored council rows store the manifest
    NESTED under row["manifest"] ({task_id, passed, manifest:{...}}), while
    flag_systemic_noops operates on FLAT manifest dicts. The CLI must pass the
    extracted manifests, not whole rows -- else every key reads False and all
    five components are falsely flagged as systemic no-ops. The original
    contract test above used flat dicts and missed this shape mismatch."""
    h = _h()
    prod_rows = [
        {"task_id": f"t{i}", "passed": True,
         "manifest": {"council_seats_ran": True, "verify_gate_ran": True,
                      "brain_recall_hit": True, "skills_loaded": True,
                      "query_tool_used": False}}
        for i in range(3)
    ]
    # Correct: extract manifests -> only the all-False conditional key flags.
    assert h.flag_systemic_noops([r["manifest"] for r in prod_rows]) == ["query_tool_used"]
    # Guard the trap: passing whole rows top-level-.get()s miss the nested keys
    # -> read False everywhere -> the always-expected bucket is mis-flagged.
    buggy = h.flag_systemic_noops(prod_rows)
    assert "council_seats_ran" in buggy and "verify_gate_ran" in buggy


# ---------------------------------------------------------------------------
# SEC-28 (D-05, 18-05-PLAN.md Task 2): per-surface manifest buckets +
# install_spies' new caller-bound fence spies. `-k manifest` selector.
# ---------------------------------------------------------------------------

def _make_spy(labels: list[str]) -> SimpleNamespace:
    """A minimal call_args_list-shaped stand-in -- avoids needing a real
    MagicMock+wraps target for pure label-bucketing unit tests."""
    return SimpleNamespace(call_args_list=[SimpleNamespace(args=(label,)) for label in labels])


def test_manifest_per_surface_buckets_populate_for_exercised_surfaces():
    """notes/memories/docs/fetched/emails buckets are computed from the
    COMBINED untrusted_context_message + fence_untrusted_text label lists
    (18-05-PLAN.md's locked label->surface mapping)."""
    h = _h()

    spies = {
        "untrusted": _make_spy(["brain notes", "saved memory: pinned user facts"]),
        "agent_loop_fence": _make_spy(
            ["tool result (web_fetch)", "tool result (mcp__email__read_email)"]),
        "answer_pipeline_fence": _make_spy(["answer grounding"]),
        "deep_research_fence": _make_spy([]),
    }
    before = {"untrusted": 0, "agent_loop_fence": 0,
              "answer_pipeline_fence": 0, "deep_research_fence": 0}

    manifest = h.manifest_from_run(events=[], spies=spies, before=before)

    assert manifest["notes"] is True
    assert manifest["memories"] is True
    assert manifest["docs"] is True
    assert manifest["fetched"] is True
    assert manifest["emails"] is True


def test_manifest_per_surface_bucket_false_when_surface_not_exercised():
    """An unexercised surface's bucket stays False -- only the notes label
    was collected here, so the other four buckets must read False."""
    h = _h()

    spies = {
        "untrusted": _make_spy(["brain notes"]),
        "agent_loop_fence": _make_spy([]),
        "answer_pipeline_fence": _make_spy([]),
        "deep_research_fence": _make_spy([]),
    }
    before = {"untrusted": 0, "agent_loop_fence": 0,
              "answer_pipeline_fence": 0, "deep_research_fence": 0}

    manifest = h.manifest_from_run(events=[], spies=spies, before=before)

    assert manifest["notes"] is True
    assert manifest["memories"] is False
    assert manifest["docs"] is False
    assert manifest["fetched"] is False
    assert manifest["emails"] is False


def test_manifest_per_surface_buckets_respect_before_snapshot_delta():
    """A label already present BEFORE this problem's run (e.g. a spy shared
    across the resumable per-problem loop) must not falsely populate THIS
    problem's bucket -- only labels appended since `before`'s call_count
    snapshot feed the bucket (mirrors the existing verify/tool/untrusted
    delta convention)."""
    h = _h()

    spies = {
        "untrusted": _make_spy([]),
        "agent_loop_fence": _make_spy([]),
        "answer_pipeline_fence": _make_spy(["answer grounding"]),  # 1 total call
        "deep_research_fence": _make_spy([]),
    }
    # before=1 for answer_pipeline_fence -> the single call already happened
    # BEFORE this problem's snapshot -> zero NEW calls -> docs stays False.
    before = {"untrusted": 0, "agent_loop_fence": 0,
              "answer_pipeline_fence": 1, "deep_research_fence": 0}

    manifest = h.manifest_from_run(events=[], spies=spies, before=before)
    assert manifest["docs"] is False


def test_manifest_fetched_bucket_excludes_email_tool_result():
    """The 'fetched' bucket's tool-result check is scoped to web_fetch/
    app_api tool_types only (18-05-PLAN.md's locked mapping) -- an email
    tool-result label must bucket under 'emails', never leak into
    'fetched' via a generic 'tool result (' prefix match."""
    h = _h()

    spies = {
        "untrusted": _make_spy([]),
        "agent_loop_fence": _make_spy(["tool result (mcp__email__read_email)"]),
        "answer_pipeline_fence": _make_spy([]),
        "deep_research_fence": _make_spy([]),
    }
    before = {"untrusted": 0, "agent_loop_fence": 0,
              "answer_pipeline_fence": 0, "deep_research_fence": 0}

    manifest = h.manifest_from_run(events=[], spies=spies, before=before)
    assert manifest["emails"] is True
    assert manifest["fetched"] is False


def test_manifest_existing_keys_and_signature_unchanged():
    """Additive-only guarantee (Phase 12 locked 3-arg shape): the 5
    pre-existing keys stay present alongside the 5 new per-surface buckets;
    no escalation_fired key anywhere."""
    h = _h()
    manifest = h.manifest_from_run(events=[], spies={}, before={})

    for key in ("council_seats_ran", "verify_gate_ran", "brain_recall_hit",
                "skills_loaded", "query_tool_used"):
        assert key in manifest
    for key in ("notes", "memories", "docs", "fetched", "emails"):
        assert key in manifest
    assert "escalation_fired" not in manifest


@contextlib.contextmanager
def _snapshot_and_restore_fence_state():
    """install_spies() mutates LIVE module attributes with no built-in
    teardown (by design -- it's meant to stay installed for a whole harness
    run). Tests that call the real install_spies() must restore the
    pre-test attribute values afterward so they don't leak a MagicMock
    wrapper into every OTHER test file sharing this pytest session."""
    import src.agent_loop as agent_loop_mod
    import src.answer_pipeline as answer_pipeline_mod
    import src.council.verify as verify_mod
    import src.deep_research as deep_research_mod

    originals = [
        (agent_loop_mod, "fence_untrusted_text", agent_loop_mod.fence_untrusted_text),
        (answer_pipeline_mod, "fence_untrusted_text", answer_pipeline_mod.fence_untrusted_text),
        (deep_research_mod, "fence_untrusted_text", deep_research_mod.fence_untrusted_text),
        (verify_mod, "verify_answer", verify_mod.verify_answer),
        (seat_runner, "untrusted_context_message", seat_runner.untrusted_context_message),
        (seat_runner, "execute_tool_block", seat_runner.execute_tool_block),
    ]
    try:
        yield
    finally:
        for module, attr, value in originals:
            setattr(module, attr, value)


def test_install_spies_wraps_new_caller_bound_fence_names():
    """install_spies() must wrap agent_loop/answer_pipeline/deep_research's
    OWN bound fence_untrusted_text name (name-binding trap, same discipline
    as the existing seat_runner.untrusted_context_message spy) -- verified
    by calling through the real module attribute and observing the spy's
    call_count advance."""
    h = _h()
    with _snapshot_and_restore_fence_state():
        spies = h.install_spies()

        assert "agent_loop_fence" in spies
        assert "answer_pipeline_fence" in spies
        assert "deep_research_fence" in spies

        import src.agent_loop as agent_loop_mod
        before_n = spies["agent_loop_fence"].call_count
        agent_loop_mod.fence_untrusted_text("test label", "test content")
        assert spies["agent_loop_fence"].call_count == before_n + 1


def test_install_spies_tolerates_absent_fence_module(monkeypatch):
    """If a module is absent (None, per the try/except ImportError guard at
    module top) install_spies() must still run and simply omit that
    surface's spy key -- never raise -- while the other two fence spies
    install normally."""
    h = _h()
    monkeypatch.setattr(h, "agent_loop_mod", None, raising=False)

    with _snapshot_and_restore_fence_state():
        spies = h.install_spies()

    assert "agent_loop_fence" not in spies
    assert "answer_pipeline_fence" in spies
    assert "deep_research_fence" in spies


def test_sc_isolation_not_weakened():
    """T-12-01: the harness must REUSE code_trainer's existing subprocess+
    timeout isolation (run_tests/extract_code — same function objects, an
    identity check, not a re-implementation) and must never define a
    competing subprocess exec of its own with the timeout disabled. Expected-
    RED until scripts/ab_harness.py exists."""
    h = _h()
    import scripts.code_trainer as code_trainer

    assert h.run_tests is code_trainer.run_tests
    assert h.extract_code is code_trainer.extract_code

    src_text = inspect.getsource(h)
    assert "timeout=None" not in src_text
    assert "timeout=0" not in src_text
    assert "subprocess.run(" not in src_text  # no competing exec of its own


def test_band_pass_rate_no_shortcircuit(monkeypatch):
    """MEAS-01 (implemented 12-02-T1): screen_borderline.screen_pass_rate
    must run ALL k samples — unlike code_trainer.sample_solutions, which
    short-circuits on the first passer (Pitfall 4) — because a pass RATE
    across k samples, not best-of-N, is what borderline-band screening
    needs. Expected-RED until scripts/screen_borderline.py exists."""
    s = _screener()
    import scripts.code_trainer as code_trainer

    calls = {"n": 0}

    def fake_generate(problem, args, feedback="", model_override=None, deep=False,
                      temperature=None):
        calls["n"] += 1
        return "```python\ndef add(a, b):\n    return a + b\n```"

    monkeypatch.setattr(code_trainer, "generate", fake_generate)

    class _Args:
        timeout = 5

    rate = s.screen_pass_rate(_PROBLEM, k=5, args=_Args())
    assert calls["n"] == 5, "screen_pass_rate must run ALL k samples, no short-circuit"
    assert rate == 1.0


def test_band_adaptive_widening_reaches_n():
    """MEAS-01 (implemented 12-02-T2): the band-selection loop must widen the
    borderline band symmetrically (per RESEARCH Open Question #2 resolution)
    until it reaches target_n>=60, or fail loud if unreachable — never
    silently ship n<60. Expected-RED until scripts/screen_borderline.py's
    select_borderline_band exists."""
    s = _screener()
    # A tight [0.4, 0.6] band alone would select only the 40 mid-range
    # problems below; adaptive widening must reach beyond that to hit n>=60
    # from the wider pool of 70 near-borderline + 94 extreme problems.
    scored = (
        [{"task_id": f"mid-{i}", "pass_rate": 0.5} for i in range(40)]
        + [{"task_id": f"near-{i}", "pass_rate": 0.3} for i in range(30)]
        + [{"task_id": f"extreme-{i}", "pass_rate": 0.0} for i in range(94)]
    )
    selected = s.select_borderline_band(scored, target_n=60)
    assert len(selected) >= 60
