"""In-process three-arm Ruby/Postgres evaluation for Phase 41.

Why this exists: /api/v1/chat is a bare model passthrough (no brain/agents),
so an HTTP "council arm" would measure nothing. The validated Phase-12 method
drives `run_council` IN-PROCESS with every seat pinned to one model. This
script reuses that exact machinery (scripts/ab_harness.py) but scores answers
with the polyglot runners (real ruby / SQL execution) instead of the
Python-only scorer.

Current Spark arm shape:
  weak_baseline   -- one-shot GLM call at low reasoning.
  strong_baseline -- one-shot call to the SAME GLM weights at max reasoning.
  architecture    -- a genuine `run_council(..., escalate_model=<same GLM>)`
                     call at low reasoning, isolating architectural lift from
                     extra reasoning compute. A repair event is evidence that
                     the existing repair path fired, not a different-model
                     upgrade.

The legacy two-model Ollama shape remains available: the architecture arm is a
genuine `run_council(..., escalate_model=<strong>)`
                     call: the council seats (still pinned WEAK via
                     `H._pinned_resolve`) synthesize normally; on
                     `needs_repair` (driven by Plan 01's real Ruby
                     EXECUTION-tier verify gate), the single existing repair
                     round swaps to the strong model and `run_council` emits
                     a genuine `escalation_fired` event (RMEAS-02, Phase 40).
                     There is no separate, non-escalating "council" arm any
                     more -- the escalation arm IS the architecture arm for
                     this phase (D-03's three-arm structure supersedes
                     Phase-12-era weak/strong-agnostic "council" naming).

Run from the repo root with the app venv. For SparkLab, establish the private
SSH tunnel first and pass `--backend openai --base-url ...` explicitly.

  .\venv\Scripts\python.exe scripts\polyglot_ab\council_arm.py ^
      --dataset scripts\polyglot_ab\datasets\ruby.jsonl ^
      --weak-model vera-coder --strong-model qwen3-coder:30b --sql-backend sqlite

Then McNemar (the D-06/D-07-mandated pairing):
  .\venv\Scripts\python.exe scripts\ab_report.py --baseline <..._weak_baseline.jsonl> --withbrain <..._escalation.jsonl>
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))   # scripts/polyglot_ab -> scripts -> repo root
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

# Importing ab_harness swaps seat_runner._resolve so every council seat resolves
# to the pinned (url, model, temp) — the same-model-both-arms invariant (D-02).
import scripts.ab_harness as H          # noqa: E402
from src.llm_core import llm_call_async  # noqa: E402
import harness as polyglot_harness       # noqa: E402
from runners import score                # noqa: E402

SYSTEM = {
    "ruby": "You are an expert Ruby developer. Respond with ONLY one ```ruby code block that defines exactly what is asked. No prose.",
    "postgres": "You are an expert PostgreSQL developer. Respond with ONLY one ```sql code block containing a single SELECT that answers the task. No prose, no DDL.",
}


def _task_prompt(problem: dict) -> str:
    """Pattern 3 (41-RESEARCH.md): prefix the SAME language+fence instruction
    the baseline arms already use (via SYSTEM[...] as a system message) onto
    the council/escalation arm's prompt, since `run_council`'s own
    `seat_runner._build_messages` has no language-specific instruction of its
    own. A pure FORMAT instruction, not a semantic reframing -- does not
    re-trip Phase-12 Pitfall 6 (brain-recall query drift)."""
    return f"{SYSTEM[problem['language']]}\n\nTASK:\n{problem['prompt']}"


async def run_baseline(problem: dict, model: str | None = None,
                       reasoning_effort: str | None = None) -> str:
    """Bare model, one shot. `model=None` (default) preserves the original
    weak-baseline behavior: pinned to `H.PINNED_MODEL` -- the SAME model the
    council seats resolve to via `H._pinned_resolve` (D-02's same-model
    invariant for the weak arm). Passing `model` explicitly (the strong
    baseline arm) drives an INDEPENDENT one-shot call WITHOUT touching
    `H.PINNED_MODEL` -- overwriting that module-level pin would also change
    what the council seats resolve to (`H._pinned_resolve` always returns
    `H.PINNED_MODEL`), silently turning the escalation arm's "weak seats,
    strong repair" contrast into "strong seats, strong repair"."""
    messages = [
        {"role": "system", "content": SYSTEM[problem["language"]]},
        {"role": "user", "content": problem["prompt"]},
    ]
    return await llm_call_async(H.PINNED_URL, model or H.PINNED_MODEL, messages,
                                temperature=H.PINNED_TEMP, headers=H.PINNED_HEADERS,
                                reasoning_effort=reasoning_effort)


def _arm_fingerprint(*, backend: str, endpoint: str, weak_model: str,
                     strong_model: str, arm: str,
                     reasoning_effort: str | None,
                     sql_backend: str) -> str:
    """Hash the complete safe experiment identity; never persist endpoint."""
    identity = {
        "arm": arm,
        "backend": backend,
        "endpoint": endpoint,
        "reasoning_effort": reasoning_effort or "",
        "sql_backend": sql_backend,
        "strong_model": strong_model,
        "temperature": H.PINNED_TEMP,
        "weak_model": weak_model,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _prepare_results(path: str, fingerprint: str,
                     valid_ids: set[str]) -> set[str]:
    """Keep only resumable rows for this exact experiment configuration."""
    existing: dict[str, dict] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as result_file:
            for line in result_file:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("run_fingerprint") == fingerprint
                        and row.get("task_id") in valid_ids):
                    existing[row["task_id"]] = row
    with open(path, "w", encoding="utf-8") as result_file:
        for row in existing.values():
            result_file.write(json.dumps(row) + "\n")
    return set(existing)


def _result_metadata(*, backend: str, model: str,
                     reasoning_effort: str | None,
                     fingerprint: str) -> dict:
    return {
        "backend": backend,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "run_fingerprint": fingerprint,
    }


def _seats_ran(events) -> bool:
    running = {e["seat_id"] for e in events
               if e.get("type") == "seat_status" and e.get("status") == "running"}
    terminal = {e["seat_id"] for e in events
                if e.get("type") == "seat_status" and e.get("status") in ("done", "error")}
    return bool(running) and running <= terminal


def manifest_from_escalation_run(events: list[dict], spies: dict, before: dict) -> dict:
    """The NEW escalation-arm manifest (Pattern 2, 41-RESEARCH.md "Component
    Manifest Design") -- layers `escalation_fired`/`escalation_failed` on top
    of the EXISTING, Phase-12-locked `H.manifest_from_run` (reused, never
    duplicated: council_seats_ran/verify_gate_ran/brain_recall_hit/
    skills_loaded/query_tool_used/the 5 SEC-28 per-surface buckets all come
    from there, unmodified). Both new keys are derived from the SAME `events`
    list the base manifest already reads -- NEVER a spy on
    `orchestrator.run_seat`/`stream_seat` (Pitfall 5b's name-binding trap)."""
    manifest = dict(H.manifest_from_run(events, spies, before))
    manifest["escalation_fired"] = any(e.get("type") == "escalation_fired" for e in events)
    manifest["escalation_failed"] = any(e.get("type") == "escalation_failed" for e in events)
    return manifest


async def _run_one_problem(p: dict, registry, cfg, spies: dict, sql_backend: str,
                           strong_model: str, weak_out: str, strong_out: str,
                           esc_out: str, *, backend: str,
                           weak_reasoning: str | None,
                           strong_reasoning: str | None,
                           architecture_reasoning: str | None,
                           fingerprints: dict[str, str],
                           run_weak: bool = True, run_strong: bool = True,
                           run_architecture: bool = True,
                           ) -> tuple[bool | None, bool | None, bool | None, dict]:
    """Run all THREE arms for one problem and append their scored rows to the
    resumable weak/strong/architecture JSONL outputs. Each already-complete
    arm is skipped independently, so recovery never duplicates a surviving
    row from another arm."""
    task_prompt = _task_prompt(p)

    wpass = None
    if run_weak:
        try:
            wtext = await run_baseline(p, reasoning_effort=weak_reasoning)
            wpass, _ = score(wtext, p, sql_backend=sql_backend)
        except Exception as e:
            wpass = False
            print(f"  [weak baseline error] {p['task_id']}: {e}")
        H.append_result(weak_out, {
            "task_id": p["task_id"], "passed": bool(wpass),
            "arm": "weak_baseline",
            **_result_metadata(backend=backend, model=H.PINNED_MODEL,
                               reasoning_effort=weak_reasoning,
                               fingerprint=fingerprints["weak"]),
        })

    spass = None
    if run_strong:
        try:
            stext = await run_baseline(p, model=strong_model,
                                       reasoning_effort=strong_reasoning)
            spass, _ = score(stext, p, sql_backend=sql_backend)
        except Exception as e:
            spass = False
            print(f"  [strong baseline error] {p['task_id']}: {e}")
        H.append_result(strong_out, {
            "task_id": p["task_id"], "passed": bool(spass),
            "arm": "strong_baseline",
            **_result_metadata(backend=backend, model=strong_model,
                               reasoning_effort=strong_reasoning,
                               fingerprint=fingerprints["strong"]),
        })

    epass = None
    manifest = {}
    if run_architecture:
        before = {name: spy.call_count for name, spy in spies.items()}
        try:
            events, final = await H.run_real_arm(
                p, registry, cfg, escalate_model=strong_model,
                prompt=task_prompt, reasoning_effort=architecture_reasoning)
            epass, _ = score(final, p, sql_backend=sql_backend)
            manifest = manifest_from_escalation_run(events, spies, before)
        except Exception as e:
            epass = False
            manifest = {"council_seats_ran": False, "verify_gate_ran": False,
                        "escalation_fired": False, "escalation_failed": False,
                        "error": f"{type(e).__name__}: {e}"}
            print(f"  [architecture error] {p['task_id']}: {e}")
        H.append_result(esc_out, {
            "task_id": p["task_id"], "passed": bool(epass),
            "arm": "architecture", "manifest": manifest,
            **_result_metadata(backend=backend, model=H.PINNED_MODEL,
                               reasoning_effort=architecture_reasoning,
                               fingerprint=fingerprints["architecture"]),
        })

    return wpass, spass, epass, manifest


def _safe(name: str) -> str:
    return name.replace(":", "_").replace("/", "_")


def _arm_status(value: bool | None) -> str:
    if value is None:
        return "SKIP"
    return "PASS" if value else "FAIL"


def _print_escalation_transparency(weak_model: str, strong_model: str,
                                   manifests: list[dict]) -> None:
    """The escalation-aware transparency block (41-RESEARCH.md "Component
    Manifest Design" point 4) -- names the weak->strong pairing and prints
    the measured `escalation_fired` rate, the manifest evidence RMEAS-03
    requires. Buckets `escalation_fired` as CONDITIONAL (flag only if False
    across ALL rows -- mirrors `query_tool_used`'s two-bucket treatment;
    `needs_repair` legitimately varies per problem)."""
    total = len(manifests)
    fired = sum(1 for m in manifests if m.get("escalation_fired"))
    print("=" * 72)
    print("TRANSPARENCY: architecture arm")
    print(f"  weak -> strong: {weak_model} -> {strong_model}")
    print(f"  repair_round_fired: {fired}/{total} rows")
    if total and fired == 0:
        print("[i] escalation_fired False on ALL rows -- conditional systemic "
              "flag (mirrors query_tool_used; needs_repair legitimately varies "
              "per problem, but zero across every row is worth confirming "
              "before sign-off).")

    flags = H.flag_systemic_noops(manifests)
    always_flagged = [f for f in flags if f in H.ALWAYS_EXPECTED_KEYS]
    cond_flagged = [f for f in flags if f in H.CONDITIONAL_KEYS]
    if always_flagged:
        print(f"[!!] CRITICAL: always-expected component(s) no-op'd on the "
              f"escalation arm: {always_flagged} -- the real arm did NOT "
              f"genuinely exercise these; the number is NOT trustworthy.")
    if cond_flagged:
        print(f"[i] conditional component(s) False across ALL escalation "
              f"rows: {cond_flagged}")
    print("=" * 72)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--backend", default="ollama", choices=["ollama", "openai"])
    ap.add_argument("--base-url",
                    help="OpenAI-compatible host, /v1 base, or full chat endpoint")
    ap.add_argument("--api-key-env", default=polyglot_harness.OPENAI_API_KEY_ENV,
                    help="name of the environment variable containing the API key")
    ap.add_argument("--weak-model", default="vera-coder",
                    help="the weak model (baseline arm + council seats via "
                         "H._pinned_resolve; D-03)")
    ap.add_argument("--strong-model", default="qwen3-coder:30b",
                    help="the strong model (baseline arm + the escalation "
                         "repair-round target; D-03)")
    ap.add_argument("--weak-reasoning", choices=polyglot_harness.REASONING_EFFORTS)
    ap.add_argument("--strong-reasoning", choices=polyglot_harness.REASONING_EFFORTS)
    ap.add_argument("--architecture-reasoning",
                    choices=polyglot_harness.REASONING_EFFORTS,
                    help="reasoning effort for all Council calls; use the weak "
                         "effort to isolate architecture from compute budget")
    ap.add_argument("--sql-backend", default="postgres", choices=["postgres", "sqlite"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-precondition", action="store_true",
                    help="bypass the fastembed/brain guard (degraded — numbers not trustworthy)")
    a = ap.parse_args()

    H.PINNED_MODEL = a.weak_model
    if a.backend == "openai":
        configured_base = a.base_url or os.getenv(
            polyglot_harness.OPENAI_BASE_URL_ENV,
            "http://127.0.0.1:1919/v1",
        )
        H.PINNED_URL = polyglot_harness.openai_chat_url(configured_base)
        api_key = polyglot_harness._api_key_from_env(a.api_key_env)
        H.PINNED_HEADERS = ({"Authorization": f"Bearer {api_key}"}
                            if api_key else {})
    else:
        H.PINNED_URL = os.getenv("PINNED_URL", H.PINNED_URL)
        H.PINNED_HEADERS = {}

    if a.skip_precondition:
        print("[!] --skip-precondition: brain may be degraded; result not a trustworthy A/B.")
    else:
        H.check_preconditions()   # fails loud if fastembed/brain aren't ready

    cfg = H.build_harness_council()
    registry = H._build_registry()
    spies = H.install_spies()

    probs = [json.loads(l) for l in open(a.dataset, encoding="utf-8") if l.strip()]
    if a.limit:
        probs = probs[:a.limit]

    weak_safe = _safe(a.weak_model + "_" + (a.weak_reasoning or "default"))
    strong_safe = _safe(a.strong_model + "_" + (a.strong_reasoning or "default"))
    stem = os.path.splitext(os.path.basename(a.dataset))[0]
    outdir = os.path.join(HERE, "results")
    os.makedirs(outdir, exist_ok=True)
    weak_out = os.path.join(outdir, f"{weak_safe}_{stem}_weak_baseline.jsonl")
    strong_out = os.path.join(outdir, f"{strong_safe}_{stem}_strong_baseline.jsonl")
    esc_out = os.path.join(outdir, f"{weak_safe}_to_{strong_safe}_{stem}_escalation.jsonl")

    fingerprints = {
        "weak": _arm_fingerprint(
            backend=a.backend, endpoint=H.PINNED_URL,
            weak_model=a.weak_model, strong_model=a.strong_model,
            arm="weak_baseline", reasoning_effort=a.weak_reasoning,
            sql_backend=a.sql_backend,
        ),
        "strong": _arm_fingerprint(
            backend=a.backend, endpoint=H.PINNED_URL,
            weak_model=a.weak_model, strong_model=a.strong_model,
            arm="strong_baseline", reasoning_effort=a.strong_reasoning,
            sql_backend=a.sql_backend,
        ),
        "architecture": _arm_fingerprint(
            backend=a.backend, endpoint=H.PINNED_URL,
            weak_model=a.weak_model, strong_model=a.strong_model,
            arm="architecture", reasoning_effort=a.architecture_reasoning,
            sql_backend=a.sql_backend,
        ),
    }
    valid_ids = {problem["task_id"] for problem in probs}
    already_weak = _prepare_results(weak_out, fingerprints["weak"], valid_ids)
    already_strong = _prepare_results(strong_out, fingerprints["strong"], valid_ids)
    already_esc = _prepare_results(
        esc_out, fingerprints["architecture"], valid_ids)
    todo = [p for p in probs
            if p["task_id"] not in already_weak
            or p["task_id"] not in already_strong
            or p["task_id"] not in already_esc]
    print(f"running {len(todo)}/{len(probs)} problems "
          f"({len(probs) - len(todo)} already scored on all three arms, resuming)")

    nw = ns = ne = 0
    for p in todo:
        run_weak = p["task_id"] not in already_weak
        run_strong = p["task_id"] not in already_strong
        run_architecture = p["task_id"] not in already_esc
        wpass, spass, epass, manifest = await _run_one_problem(
            p, registry, cfg, spies, a.sql_backend, a.strong_model,
            weak_out, strong_out, esc_out, backend=a.backend,
            weak_reasoning=a.weak_reasoning,
            strong_reasoning=a.strong_reasoning,
            architecture_reasoning=a.architecture_reasoning,
            fingerprints=fingerprints, run_weak=run_weak,
            run_strong=run_strong, run_architecture=run_architecture)
        nw += 1 if wpass else 0
        ns += 1 if spass else 0
        ne += 1 if epass else 0
        print(f"  {p['task_id']:30} weak={_arm_status(wpass)}  "
              f"strong={_arm_status(spass)}  "
              f"architecture={_arm_status(epass)}  "
              f"escalation_fired={manifest.get('escalation_fired')}")

    n = len(probs)
    print("\n" + "=" * 56)
    print(f"  WEAK BASELINE   : {nw}/{len(todo) or n} scored this run")
    print(f"  STRONG BASELINE : {ns}/{len(todo) or n} scored this run")
    print(f"  ARCHITECTURE    : {ne}/{len(todo) or n} scored this run")
    print("=" * 56)

    esc_rows = [json.loads(l) for l in open(esc_out, encoding="utf-8") if l.strip()]
    manifests = [r.get("manifest", {}) for r in esc_rows]
    _print_escalation_transparency(a.weak_model, a.strong_model, manifests)

    print("\nMcNemar A/B report (architecture lift, low baseline vs low Council):")
    print(f"  python scripts\\ab_report.py --baseline {weak_out} --withbrain {esc_out}")
    print("\nMcNemar A/B report (discretionary context, weak vs strong):")
    print(f"  python scripts\\ab_report.py --baseline {weak_out} --withbrain {strong_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
