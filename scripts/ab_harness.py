"""A/B measurement harness (MEAS-01/MEAS-02, Phase 12 plans 12-03/12-04): the
pinned-config bare-baseline arm, the real-architecture arm that drives a
genuine `run_council` per problem, the fastembed/brain precondition guard,
the resumable JSONL writer, the per-run component-touch manifest (D-04 leg
1), and the CLI `main()` that ties the borderline band (12-02) -> both arms
-> two JSONL files -> the UNCHANGED `scripts/ab_report.py` (12-04).

Landmines this file deliberately encodes (see .planning/phases/
12-effectiveness-measurement-a-b/12-RESEARCH.md):

  - `DEFAULT_COUNCIL`-shaped seats ship `endpoint_id=""`/`model=""` ->
    `seat_runner._resolve` returns `None` -> every seat fails "no model
    configured" (Pitfall 2). `build_harness_council()` builds its OWN
    minimal `CouncilConfig` and `_pinned_resolve` is swapped onto
    `seat_runner._resolve` at import time. `src.llm_core.llm_call_async` /
    `stream_llm` are left LIVE (never patched here) so the real arm makes
    genuine model calls (Pattern 1).
  - Minimal 2-seat council only -- 1 panelist + 1 synthesizer (D-01) -- NOT
    the full 7-seat `DEFAULT_COUNCIL`, which would serialize per-problem
    model calls into hours against a single local GPU at n>=60 (Pitfall 3).
    This module's own CLI (`main()` below) never passes `escalate_model` --
    Phase 12's own exercised-component claim stays escalation-free. Phase 41
    (41-06) added an ADDITIVE, opt-in `escalate_model`/`prompt` kwarg pair to
    `run_real_arm` so OTHER callers (`scripts/polyglot_ab/council_arm.py`)
    can drive the Phase-40 `run_council(escalate_model=...)` primitive
    through this same pinned-endpoint machinery; `manifest_from_run` below
    still carries no `escalation_fired` key (see that function's own
    docstring) -- a caller wanting that signal builds its own manifest
    function layered on top (Pattern 2), never edits this locked one.
  - The `done` event's HONEST `verified` key is read, never
    `done["verification"]["passed"]` (SEC-08/Phase 11 false-assurance bug).
  - The baseline arm is pinned to the IDENTICAL (url, model, temperature,
    headers) the council seats resolve to via `_pinned_resolve` -- same
    model both arms, so any pass@1 delta is attributable to the
    architecture, not the model (D-02).
  - `council_seats_ran` is derived by callers from the `seat_status` EVENT
    STREAM `run_real_arm` returns, never a `run_seat` spy --
    `orchestrator.py` name-binds `run_seat`/`stream_seat` into its own
    module namespace at its own module top, so a spy on
    `seat_runner.run_seat` would be invisible to it (Pitfall 5b, Pattern 2).
  - Resumable JSONL writer: append-mode + skip-already-scored `task_id`,
    never `code_trainer.py`'s truncate-on-open `"w"` convention -- an
    interrupted n>=60 council run must resume, not restart from zero.

  - `extract_code`/`run_tests` are re-exported by IDENTITY from
    `scripts.code_trainer` (imported, never re-implemented) so BOTH arms'
    answers are scored with the exact same subprocess+timeout isolation
    (T-12-01) -- this file defines no competing `subprocess.run` of its own.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

# Runnable as `python scripts/ab_harness.py`: Python puts scripts/ on sys.path,
# not the repo root, so the `import src.*` below would raise ModuleNotFoundError.
# Prepend the repo root. (pytest already runs with it on the path; E402 is
# ignored for scripts/* in pyproject.toml.)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.council.verify as verify_mod
from src.council import seat_runner
from src.council.orchestrator import run_council
from src.council.schemas import CouncilConfig, Seat
from src.llm_core import llm_call_async

# SEC-28 (D-05, 18-05-PLAN.md Task 2): the three NEW caller-bound
# fence_untrusted_text call sites added by Plans 02-03. Imported defensively
# (try/except ImportError) -- install_spies() below guards on both the
# module being importable AND the attribute being present, so the harness
# still runs (just without that surface's manifest bucket populated) if a
# future refactor renames/removes one of these.
try:
    import src.agent_loop as agent_loop_mod
except ImportError:
    agent_loop_mod = None

try:
    import src.answer_pipeline as answer_pipeline_mod
except ImportError:
    answer_pipeline_mod = None

try:
    import src.deep_research as deep_research_mod
except ImportError:
    deep_research_mod = None

try:
    import scripts.code_trainer as code_trainer
except ImportError:
    # Namespace-package import (PEP 420, scripts/ has no __init__.py) can fail
    # depending on how this module is loaded (e.g. spec_from_file_location by
    # a caller); fall back to the same dynamic-load convention
    # screen_borderline.py already uses for the identical situation.
    _here = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.util.spec_from_file_location(
        "code_trainer", os.path.join(_here, "code_trainer.py"))
    code_trainer = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(code_trainer)

# Reused VERBATIM (identity, not a wrapper) -- T-12-01: both arms' generated
# code is scored via code_trainer's own subprocess+timeout-isolated scorer.
extract_code = code_trainer.extract_code
run_tests = code_trainer.run_tests

# ---------------------------------------------------------------------------
# Pinned constants (RESEARCH Pattern 1 / A1) -- both arms decode at the
# IDENTICAL (url, model, temperature, headers) so a measured pass@1 delta is
# attributable to the architecture (brain + council seats + skills +
# query-tool surface + verify), not the model.
# ---------------------------------------------------------------------------
# code_trainer.py's --ollama-url default is "http://127.0.0.1:11434/v1"; its
# generate() posts to ollama_url.rstrip("/") + "/chat/completions" -- pinned
# here as the full URL so both arms hit the SAME live local Ollama endpoint.
PINNED_URL = "http://127.0.0.1:11434/v1/chat/completions"
PINNED_MODEL = "vera-coder"      # code_trainer.py's --model default
PINNED_TEMP = 0.2                # code_trainer.py's generate() non-deep default
PINNED_HEADERS: dict = {}
# Fixed fake identity (T-12-03): run_query's owner_can_manage_data_sources
# gate denies predictably -- never a real user session.
FAKE_OWNER = "ab-harness"


def _pinned_resolve(seat: Seat, owner: str | None):
    """Bypass DB-backed endpoint resolution (Pitfall 2): a harness-local
    `CouncilConfig` has no `ModelEndpoint` DB row to resolve against, and a
    `DEFAULT_COUNCIL`-shaped seat's `endpoint_id=""`/`model=""` would make the
    real `_resolve` short-circuit to `None` before it even queries the DB.
    Every harness seat resolves to the SAME pinned local Ollama endpoint
    regardless of its own `endpoint_id`/`model` fields. `llm_call_async`/
    `stream_llm` themselves are untouched, so calls made through this
    resolution are genuinely live."""
    return (PINNED_URL, PINNED_MODEL, PINNED_HEADERS)


# Module-level swap for the harness process lifetime (Pattern 1) -- installed
# at import time so any run_council call made after `import scripts.
# ab_harness` already resolves seats through the pinned endpoint.
seat_runner._resolve = _pinned_resolve


def build_harness_council() -> CouncilConfig:
    """Minimal 2-seat council (D-01, Pitfall 3): 1 panelist owning
    `skills=["code-review"]` (so the skill-guidance untrusted wrap fires) and
    `tools=["run_query"]` (so the query-tool dispatch surface is
    live/observable, even though HumanEval problems rarely trigger it --
    Pitfall 7) + 1 synthesizer that consumes it. Both seats pinned at
    `PINNED_TEMP` so the decoding params are airtight-identical to the
    baseline arm regardless of `Seat`'s own schema default. NOT the full
    7-seat `DEFAULT_COUNCIL`, which would serialize per-problem model calls
    into hours against a single local GPU at n>=60. No escalation wrap: see
    module docstring."""
    return CouncilConfig(seats=[
        Seat(id="analyst", role="panelist", skills=["code-review"],
             tools=["run_query"], consumes=[], temperature=PINNED_TEMP),
        Seat(id="chief", role="synthesizer", consumes=["analyst"],
             temperature=PINNED_TEMP),
    ])


def _fastembed_available() -> bool:
    """Probe: is fastembed importable? Test-monkeypatched to `False` to force
    the precondition-absence path
    (`test_precondition_fails_loud_when_fastembed_absent`)."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


def _brain_index_available(index_dir: str = "brains/llm-council/index") -> bool:
    """Probe: does the production brain index look present/loadable? Checks
    for `meta.json` (the index's own identity-stamp file, GRAY-BRN-01)
    without doing a full `BrainIndex()` load -- this stays a cheap startup
    guard, not a second index load."""
    return os.path.isfile(os.path.join(index_dir, "meta.json"))


def check_preconditions() -> None:
    """MEAS-01 precondition guard: fail loud (never silently proceed) when
    fastembed is unimportable or the production brain index is absent --
    either would silently downgrade recall to half-strength (or nothing) and
    understate what "the real architecture" measures. Implemented for
    portability: this environment already satisfies both (RESEARCH.md
    Environment Availability), so this only actually raises when a test
    forces absence via monkeypatching `_fastembed_available`/
    `_brain_index_available`."""
    if not _fastembed_available():
        raise RuntimeError(
            "fastembed is not importable -- a trustworthy MEAS-01 number "
            "needs brain recall at full strength: pip install fastembed"
        )
    if not _brain_index_available():
        raise RuntimeError(
            "production brain index not found (brains/llm-council/index/"
            "meta.json missing) -- a trustworthy MEAS-01 number needs the "
            "brain reindexed: python scripts/reindex_brain.py"
        )


# ---------------------------------------------------------------------------
# Resumable JSONL writer -- delta from code_trainer.py's truncate-on-open
# ("w") output-file convention (Anti-Pattern, RESEARCH.md).
# ---------------------------------------------------------------------------

def already_scored(path: str) -> set[str]:
    """The set of `task_id` already present in an existing result JSONL
    (empty if the file is absent) -- lets an interrupted n>=60 run resume by
    skipping already-scored problems instead of restarting from zero."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {json.loads(line)["task_id"] for line in f if line.strip()}


def append_result(path: str, row: dict) -> None:
    """Append ONE scored row -- never truncate-on-open, so a run interrupted
    at problem 40 doesn't lose problems 0-39 on restart."""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Baseline arm: one-shot llm_call_async at the pinned (url, model, temp,
# headers) -- the SAME transport + decoding params the council seats resolve
# to via `_pinned_resolve` (D-02).
# ---------------------------------------------------------------------------

async def run_baseline(problem: dict,
                       reasoning_effort: str | None = None) -> str:
    """Bare baseline: a single one-shot `llm_call_async` call, zero
    augmentation (no brain, no council, no skills, no verify). Pinned to the
    IDENTICAL (url, model, temperature, headers) as the council seats so any
    pass@1 delta between arms is attributable to the architecture, not the
    model."""
    messages = [
        {"role": "system", "content": "You are an expert Python engineer. "
                                       "Complete the function. Return ONLY a "
                                       "Python code block, no prose."},
        {"role": "user", "content": problem["prompt"]},
    ]
    return await llm_call_async(PINNED_URL, PINNED_MODEL, messages,
                                temperature=PINNED_TEMP, headers=PINNED_HEADERS,
                                reasoning_effort=reasoning_effort)


# ---------------------------------------------------------------------------
# Real arm: drives a genuine run_council per problem.
# ---------------------------------------------------------------------------

async def run_real_arm(problem: dict, registry, cfg: CouncilConfig,
                       owner: str = FAKE_OWNER,
                       escalate_model: str | None = None,
                       prompt: str | None = None,
                       reasoning_effort: str | None = None) -> tuple[list[dict], str]:
    """Drive ONE live `run_council` call over `problem["prompt"]` (RAW,
    unmodified -- Pitfall 6: a "please solve this HumanEval problem" preamble
    would drift the brain recall query away from the note content) with the
    minimal harness council, the REAL production brain `registry`, and
    `verify_execute=True` (the verify gate actually runs the code, not just a
    syntax check). Returns `(events, final)` -- `final` is the honest
    `done["final"]` text; callers score it via `extract_code`/`run_tests`
    (this module's re-exports). Reads `done["verified"]`, NEVER
    `done["verification"]["passed"]` (SEC-08/Phase 11 false-assurance bug).
    `council_seats_ran` is deliberately NOT computed here -- callers derive it
    from the returned `events`' `seat_status` entries (Pattern 2), since a
    `run_seat` spy would be invisible to `orchestrator.py`'s own call sites
    (Pitfall 5b).

    Phase 41 (41-06, additive-only -- no existing caller passes either kwarg,
    so default behavior is byte-for-byte unchanged):
      `escalate_model` -- threaded straight into `run_council`'s own
        per-request weak->strong repair-round swap (RMEAS-02, Phase 40).
        Falsy (the default `None`) stays fully inert, exactly as
        `run_council` itself documents.
      `prompt` -- an explicit override for the text sent to `run_council`,
        used INSTEAD of `problem["prompt"]` when given (`None`, the default,
        preserves the RAW-prompt behavior above). This is for callers like
        `scripts/polyglot_ab/council_arm.py` that need to prefix a language+
        fence FORMAT instruction (Pattern 3) onto the prompt for a
        non-Python band -- a pure formatting change, not the semantic
        "please solve this benchmark problem" reframing Pitfall 6 warns
        against."""
    events: list[dict] = []
    council_prompt = prompt if prompt is not None else problem["prompt"]
    async for ev in run_council(council_prompt, cfg, owner=owner,
                                registry=registry, verify_execute=True,
                                fresh_context=True, escalate_model=escalate_model,
                                reasoning_effort=reasoning_effort):
        events.append(ev)
    done = next(e for e in events if e["type"] == "done")
    return events, done["final"]


# ---------------------------------------------------------------------------
# Component-touch manifest (D-04 leg 1, 12-04-T1) -- TWO buckets (Pitfall 7,
# LOCKED): always-expected (council_seats_ran, verify_gate_ran) flagged if
# False on ANY scored run; conditional (brain_recall_hit, skills_loaded,
# query_tool_used) flagged ONLY if False across ALL scored runs. NO
# escalation_fired field anywhere (D-01 amendment -- escalation is not wired
# on the council path, Phase 29/GRAY-AGT-04, and a permanently-False field
# presented as if it could fire is forbidden).
# ---------------------------------------------------------------------------

# Keys the manifest MUST fire True on every scored run -- a False here is a
# real problem worth flagging PER-ROW (not just systemically).
ALWAYS_EXPECTED_KEYS = ("council_seats_ran", "verify_gate_ran")
# Keys that legitimately vary per problem (Pitfall 6/7) -- only flag when
# False across EVERY scored row (a systemic non-wiring signal).
CONDITIONAL_KEYS = ("brain_recall_hit", "skills_loaded", "query_tool_used")


def install_spies() -> dict:
    """Install `MagicMock(wraps=...)` spies on the LIVE attributes the
    per-run manifest observes (Pattern 3), kept installed for the WHOLE
    harness run (not per-problem): `verify_mod.verify_answer` at its ORIGIN
    (`src/verified_gate.py` imports it by MODULE reference, so patching the
    origin module's attribute is visible there); `seat_runner.
    untrusted_context_message` / `seat_runner.execute_tool_block` at the
    CALLER `seat_runner` (their own name-bound imports -- patching
    `src.prompt_security`/`src.tool_execution` origins would be a silent
    no-op, Pitfall 5). Returns the spies keyed by name; callers snapshot
    each spy's `call_count` (and the label-collecting spies' `call_args_
    list` length) before each problem and pass that snapshot to
    `manifest_from_run` as `before`.

    SEC-28 (D-05, 18-05-PLAN.md Task 2): also installs spies on the three
    NEW caller-bound `fence_untrusted_text` call sites added by Plans 02-03
    -- `agent_loop.fence_untrusted_text`, `answer_pipeline.
    fence_untrusted_text`, `deep_research.fence_untrusted_text` -- same
    name-binding-trap discipline as the `untrusted_context_message` spy
    above (patch the CALLING module's own bound name, never `src.
    prompt_security.fence_untrusted_text` directly). Each is guarded by
    `module is not None and hasattr(module, "fence_untrusted_text")` so the
    harness still runs (just without that surface's manifest bucket
    populated) if a module or attribute is absent."""
    verify_spy = MagicMock(wraps=verify_mod.verify_answer)
    verify_mod.verify_answer = verify_spy

    untrusted_spy = MagicMock(wraps=seat_runner.untrusted_context_message)
    seat_runner.untrusted_context_message = untrusted_spy

    tool_spy = MagicMock(wraps=seat_runner.execute_tool_block)
    seat_runner.execute_tool_block = tool_spy

    spies = {"verify": verify_spy, "untrusted": untrusted_spy, "tool": tool_spy}

    for key, module in (
        ("agent_loop_fence", agent_loop_mod),
        ("answer_pipeline_fence", answer_pipeline_mod),
        ("deep_research_fence", deep_research_mod),
    ):
        if module is not None and hasattr(module, "fence_untrusted_text"):
            fence_spy = MagicMock(wraps=module.fence_untrusted_text)
            module.fence_untrusted_text = fence_spy
            spies[key] = fence_spy

    return spies


def _tool_result_type(label: str) -> str | None:
    """Extract the tool_type from an agent_loop `"tool result (<type>)"`
    fence label (18-05-PLAN.md's `_maybe_fence_tool_result` label shape), or
    None if `label` isn't shaped that way."""
    if label.startswith("tool result (") and label.endswith(")"):
        return label[len("tool result ("):-1]
    return None


# SEC-28 (D-05) locked label -> surface mapping (18-05-PLAN.md), reused
# verbatim by the per-surface buckets below and by
# tests/test_prompt_injection_surfaces.py's routing asserts:
#   notes:    label == "brain notes" (Council) or "brain knowledge" (Chat)
#   memories: label.startswith("saved memory:") or "episodic memory:"
#   docs:     label == "retrieved documents" (Chat RAG) or "answer grounding" (Answer)
#   fetched:  label.startswith("web page:") or == "web search results"
#             or label.startswith("deep_research webpage") or a tool-result
#             label whose tool_type is web_fetch/app_api
#   emails:   a tool-result label whose parenthesized name contains "email"
_FETCHED_TOOL_TYPES = ("web_fetch", "app_api")


def manifest_from_run(events: list[dict], spies: dict, before: dict) -> dict:
    """Build ONE problem's component-touch manifest. `events` is the
    `run_real_arm` event stream for this problem; `spies` is `install_spies`'
    return value; `before` is a `{"verify": n, "untrusted": n, "tool": n,
    "agent_loop_fence": n, "answer_pipeline_fence": n, "deep_research_fence": n}`
    snapshot of each spy's `call_count` taken immediately BEFORE this
    problem's `run_real_arm` call. `council_seats_ran`/`verify_gate_ran` are
    derived from the event stream (Pattern 2 -- never a `run_seat` spy,
    Pitfall 5b); `brain_recall_hit`/`skills_loaded`/`query_tool_used` are
    derived from the spies' call-count delta / label diff since `before`
    (Pattern 3). No `escalation_fired` key (D-01 amendment).

    SEC-28 (D-05, 18-05-PLAN.md Task 2): additionally emits five per-surface
    boolean buckets -- `notes`/`memories`/`docs`/`fetched`/`emails` -- keyed
    off the COMBINED label list (the existing `untrusted_spy` labels plus
    the three new caller-bound fence spies' labels), per the locked label->
    surface mapping above. Additive only -- the five original keys/the 3-arg
    signature are unchanged (Phase 12 locked shape)."""
    verify_spy = spies.get("verify")
    untrusted_spy = spies.get("untrusted")
    tool_spy = spies.get("tool")

    v0 = before.get("verify", 0)
    t0 = before.get("tool", 0)
    u0 = before.get("untrusted", 0)

    verify_gate_ran = bool(verify_spy is not None and verify_spy.call_count > v0)
    query_tool_used = bool(tool_spy is not None and tool_spy.call_count > t0)

    labels: list[str] = []
    if untrusted_spy is not None:
        labels = [c.args[0] for c in untrusted_spy.call_args_list[u0:]]
    brain_recall_hit = "brain notes" in labels
    skills_loaded = any(label.startswith("skill:") for label in labels)

    # SEC-28 (D-05): collect labels from the NEW caller-bound fence spies
    # (agent_loop/answer_pipeline/deep_research fence_untrusted_text),
    # combined with the existing untrusted_context_message labels above so
    # every surface's label -- regardless of which primitive it routes
    # through -- feeds the per-surface buckets below.
    fence_labels: list[str] = []
    for key in ("agent_loop_fence", "answer_pipeline_fence", "deep_research_fence"):
        fence_spy = spies.get(key)
        if fence_spy is not None:
            n0 = before.get(key, 0)
            fence_labels.extend(c.args[0] for c in fence_spy.call_args_list[n0:])
    all_labels = labels + fence_labels

    # council_seats_ran (Pattern 2): every seat that STARTED (status
    # "running") must also reach a TERMINAL status ("done"/"error") -- a
    # seat that started but never reached terminal, or no seat starting at
    # all, is not a genuine council run.
    running_ids = {e["seat_id"] for e in events
                   if e.get("type") == "seat_status" and e.get("status") == "running"}
    terminal_ids = {e["seat_id"] for e in events
                    if e.get("type") == "seat_status" and e.get("status") in ("done", "error")}
    council_seats_ran = bool(running_ids) and running_ids <= terminal_ids

    notes = any(label in ("brain notes", "brain knowledge") for label in all_labels)
    memories = any(
        label.startswith("saved memory:") or label.startswith("episodic memory:")
        for label in all_labels
    )
    docs = any(label in ("retrieved documents", "answer grounding") for label in all_labels)
    fetched = any(
        label.startswith("web page:")
        or label == "web search results"
        or label.startswith("deep_research webpage")
        or _tool_result_type(label) in _FETCHED_TOOL_TYPES
        for label in all_labels
    )
    emails = any(
        label.startswith("tool result (") and "email" in label
        for label in all_labels
    )

    return {
        "council_seats_ran": council_seats_ran,
        "verify_gate_ran": verify_gate_ran,
        "brain_recall_hit": brain_recall_hit,
        "skills_loaded": skills_loaded,
        "query_tool_used": query_tool_used,
        "notes": notes,
        "memories": memories,
        "docs": docs,
        "fetched": fetched,
        "emails": emails,
    }


def flag_systemic_noops(council_rows: list[dict]) -> list[str]:
    """Two-bucket flagging (Pitfall 7, LOCKED) over every scored council
    row's manifest: ALWAYS-EXPECTED keys are flagged if False on ANY row (a
    silent no-op on a scored run is a real problem); CONDITIONAL keys are
    flagged ONLY if False across ALL rows (a systemic non-wiring signal, not
    per-problem irrelevance -- Pitfall 6). Returns human-readable flag
    strings (bare key names); never includes an `escalation` flag."""
    flags: list[str] = []
    for key in ALWAYS_EXPECTED_KEYS:
        if any(not row.get(key, False) for row in council_rows):
            flags.append(key)
    for key in CONDITIONAL_KEYS:
        if council_rows and all(not row.get(key, False) for row in council_rows):
            flags.append(key)
    return flags


# ---------------------------------------------------------------------------
# CLI wiring (12-04-T2): band (12-02) -> both arms (resumable) -> two JSONL
# -> the UNCHANGED scripts/ab_report.py (pass@1 both arms + McNemar p,
# MEAS-02). Live-model execution only happens under `if __name__ ==
# "__main__"` below -- importing this module (e.g. for the pytest suite)
# never spawns a live Ollama call.
# ---------------------------------------------------------------------------

class _Args:
    """A minimal args namespace for run_tests/extract_code's `args.timeout`
    convention (mirrors code_trainer.py's own argparse Namespace shape)."""

    def __init__(self, timeout: int):
        self.timeout = timeout


def _load_band(path: str) -> list[dict]:
    """Read the borderline band JSONL (12-02's `select_borderline_band`
    output): one row per problem, task_id/pass_rate + the downstream
    prompt/test/entry_point/kind fields `run_baseline`/`run_real_arm` need."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _score(problem: dict, completion: str, timeout: int) -> bool:
    """Score ONE completion via `extract_code`+`run_tests` -- the SAME
    subprocess+timeout-isolated scorer both arms use (T-12-01, this module's
    identity re-exports of `scripts.code_trainer`'s own functions)."""
    code = extract_code(completion)
    ok, _detail = run_tests(problem, code, timeout)
    return bool(ok)


def _build_registry():
    """The REAL production brain registry (MEAS-01: measures the real
    reindexed brain, never a fixture vault) -- `src.brains.
    build_brain_providers()` wrapped in a `MemoryProviderRegistry`."""
    from src.brains import build_brain_providers
    from src.memory_provider import MemoryProviderRegistry
    return MemoryProviderRegistry(providers=build_brain_providers())


def _run_one_problem(problem: dict, registry, cfg: CouncilConfig, timeout: int,
                     spies: dict, baseline_out: str, council_out: str) -> None:
    """Run BOTH arms for one band problem and append their scored rows to
    the resumable baseline/council JSONL outputs. Snapshots each spy's
    call_count BEFORE the real arm's `run_council` call (Pattern 3) so
    `manifest_from_run` can diff the delta attributable to THIS problem."""
    baseline_completion = asyncio.run(run_baseline(problem))
    baseline_passed = _score(problem, baseline_completion, timeout)
    append_result(baseline_out, {"task_id": problem["task_id"], "passed": baseline_passed})

    before = {name: spy.call_count for name, spy in spies.items()}
    events, final = asyncio.run(run_real_arm(problem, registry, cfg, owner=FAKE_OWNER))
    council_passed = _score(problem, final, timeout)
    manifest = manifest_from_run(events, spies, before)
    done = next(e for e in events if e["type"] == "done")
    append_result(council_out, {
        "task_id": problem["task_id"],
        "passed": council_passed,
        "verified": bool(done["verified"]),
        "manifest": manifest,
    })


def _print_transparency_block(cfg: CouncilConfig) -> None:
    """A prominent, honest block naming exactly what the real arm exercises
    -- brain recall + council seats + skills + the query-tool dispatch
    surface + the verify gate -- and explicitly NOT escalation (D-01
    amendment: `run_council` has no escalation primitive, Phase 29/
    GRAY-AGT-04 not done)."""
    seats = [s.id for s in cfg.active_seats()]
    skills = sorted({sk for s in cfg.active_seats() for sk in (s.skills or [])})
    tools = sorted({t for s in cfg.active_seats() for t in (s.tools or [])})
    print("=" * 72)
    print("TRANSPARENCY: real-arm exercised components")
    print(f"  model (both arms): {PINNED_MODEL} @ temp {PINNED_TEMP} "
          f"(same model both arms -- D-02)")
    print(f"  council seats  : {seats}")
    print(f"  skills owned   : {skills}")
    print(f"  tools owned    : {tools}")
    print("  + brain recall (real production registry) + the verify gate")
    print("  NOT exercised  : escalation (no primitive on the council path; "
          "Phase 29/GRAY-AGT-04, D-01 amendment)")
    print("=" * 72)


def main() -> int:
    global PINNED_MODEL
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--band", default="data/evals/humaneval_band.jsonl")
    ap.add_argument("--baseline-out", default="data/evals/ab_baseline.jsonl")
    ap.add_argument("--council-out", default="data/evals/ab_council.jsonl")
    ap.add_argument("--model", default=PINNED_MODEL,
                     help="model pinned for BOTH arms (default vera-coder); the "
                          "same-model-both-arms invariant (D-02) holds "
                          "regardless of choice. Use a weaker model when the "
                          "default saturates the benchmark (no borderline band).")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--skip-precondition", action="store_true", default=False,
                     help="Skip the fastembed/brain-index precondition guard "
                          "-- ONLY for a deliberately-degraded dry run; the "
                          "resulting numbers are NOT a trustworthy MEAS-01 "
                          "result.")
    args = ap.parse_args()
    PINNED_MODEL = args.model

    if args.skip_precondition:
        print("[!] --skip-precondition set -- this is a DELIBERATELY "
              "DEGRADED dry run; the resulting pass@1/manifest numbers are "
              "NOT a trustworthy MEAS-01 measurement.")
    else:
        check_preconditions()

    problems = _load_band(args.band)
    if args.limit:
        problems = problems[:args.limit]

    spies = install_spies()
    cfg = build_harness_council()
    registry = _build_registry()

    already_base = already_scored(args.baseline_out)
    already_council = already_scored(args.council_out)
    todo = [p for p in problems
            if p["task_id"] not in already_base or p["task_id"] not in already_council]
    print(f"running {len(todo)}/{len(problems)} problems "
          f"({len(problems) - len(todo)} already scored, resuming)")

    for problem in todo:
        _run_one_problem(problem, registry, cfg, args.timeout, spies,
                         args.baseline_out, args.council_out)

    council_rows = [json.loads(line) for line in open(args.council_out, encoding="utf-8")
                   if line.strip()]
    # Each row stores its manifest NESTED under row["manifest"]; pass the
    # manifest dicts (not whole rows) so the two-bucket keys resolve.
    manifests = [r.get("manifest", {}) for r in council_rows]
    flags = flag_systemic_noops(manifests)
    always_flagged = [f for f in flags if f in ALWAYS_EXPECTED_KEYS]
    cond_flagged = [f for f in flags if f in CONDITIONAL_KEYS]
    if always_flagged:
        print(f"[!!] CRITICAL: always-expected component(s) no-op'd on a scored "
              f"run: {always_flagged} -- the real arm did NOT genuinely exercise "
              f"these; the number is NOT a trustworthy MEAS-01 result.")
    if cond_flagged:
        print(f"[i] conditional component(s) False across ALL runs: {cond_flagged}"
              f" -- often EXPECTED (query_tool_used: HumanEval coding tasks don't "
              f"query a DB; the tool surface is wired, just not triggered). Confirm "
              f"it matches the benchmark before sign-off.")
    if not flags:
        print("no systemic no-op flags -- every always-expected component fired on "
              "every scored run, and no conditional component was False on ALL runs.")

    _print_transparency_block(cfg)

    print()
    print("Run the report with:")
    print(f"  python scripts/ab_report.py --baseline {args.baseline_out} "
          f"--withbrain {args.council_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
