"""Borderline-band screener (D-03, MEAS-01): reads data/evals/humaneval.jsonl,
measures each problem's baseline pass_rate across k temperature-diverse
samples, and selects the subset the bare model lands near ~50/50 -- the band
where the real architecture (council/verify/skills) can actually flip an
outcome -- writing data/evals/humaneval_band.jsonl (task_id + pass_rate +
the downstream problem fields the harness needs).

Reuses scripts/code_trainer.py's generate/_sample_temps/extract_code/
run_tests/load_problems VERBATIM (imported, never reimplemented or copied).
Two pitfalls this file deliberately avoids (see .planning/phases/
12-effectiveness-measurement-a-b/12-RESEARCH.md):

  - Pitfall 4: code_trainer.sample_solutions() short-circuits on the FIRST
    passing sample (best-of-N) -- it cannot produce a pass RATE. This file's
    screen_pass_rate runs ALL k samples every time.
  - Pitfall 8: screening at a single low, near-deterministic temperature
    would cluster nearly every problem at pass_rate 0%/100%, leaving the
    borderline band almost empty. Screening uses code_trainer._sample_temps'
    DIVERSIFIED discovery ladder, decoupled from the scored arms' pinned low
    temperature (a separate concern, not touched by this file).

code_trainer is imported by MODULE REFERENCE (`code_trainer.generate(...)`),
never via `from code_trainer import generate` -- a name-bound import would
freeze the function object at import time, defeating any caller (test or
otherwise) that patches `code_trainer.generate` on the module itself (the
same name-binding trap this phase's RESEARCH.md documents for
seat_runner.untrusted_context_message).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time

try:
    import scripts.code_trainer as code_trainer
except ImportError:
    _here = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.util.spec_from_file_location(
        "code_trainer", os.path.join(_here, "code_trainer.py"))
    code_trainer = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(code_trainer)


class BandUnreachableError(RuntimeError):
    """Raised when the borderline band cannot reach target_n even at the
    widening cap -- a genuinely too-easy/too-hard model surfaced, not
    hidden behind a silently-tiny n."""


def screen_pass_rate(problem: dict, k: int, args) -> float:
    """Run k temperature-diverse baseline samples against `problem` (the
    DISCOVERY ladder from code_trainer._sample_temps -- decoupled from the
    scored arms' pinned low temperature, Pitfall 8) and score EACH one via
    extract_code+run_tests. Never short-circuits on the first pass (that is
    sample_solutions' bug, Pitfall 4). A flaky per-sample exception degrades
    that sample to a fail rather than aborting the whole screen. Returns the
    measured pass_rate = passes/k."""
    passes = 0
    for temp in code_trainer._sample_temps(k):
        try:
            completion = code_trainer.generate(problem, args, temperature=temp)
            code = code_trainer.extract_code(completion)
            ok, _detail = code_trainer.run_tests(problem, code, args.timeout)
        except Exception:
            ok = False
        if ok:
            passes += 1
    return passes / k


def _score_all(problems: list[dict], args) -> list[dict]:
    """Screen every problem's pass_rate once; keep just task_id + pass_rate
    so the adaptive widening step can re-select from the ALREADY-COMPUTED
    rates without re-screening."""
    scored = []
    for problem in problems:
        rate = screen_pass_rate(problem, args.k, args)
        scored.append({"task_id": problem["task_id"], "pass_rate": rate})
    return scored


def _pass_rate_histogram(scored: list[dict], buckets: int = 10) -> dict[str, int]:
    """Coarse pass_rate histogram for the fail-loud diagnostic."""
    hist: dict[str, int] = {}
    for row in scored:
        b = min(buckets - 1, int(row["pass_rate"] * buckets))
        key = f"{b / buckets:.1f}-{(b + 1) / buckets:.1f}"
        hist[key] = hist.get(key, 0) + 1
    return hist


def _widen_to_target(scored: list[dict], target_n: int, band_lo: float, band_hi: float,
                      widen_step: float, max_widen: float) -> tuple[list[dict], float, float]:
    """Widen [band_lo,band_hi] symmetrically in widen_step increments (up to
    max_widen), re-selecting from the already-computed `scored` pass_rates
    each time, until len(selected) >= target_n. Returns (selected, lo, hi).
    Raises BandUnreachableError (fail loud) if target_n is unreachable even
    at the widening cap."""
    widen = 0.0
    while widen <= max_widen + 1e-9:
        lo, hi = max(0.0, band_lo - widen), min(1.0, band_hi + widen)
        selected = [row for row in scored if lo <= row["pass_rate"] <= hi]
        if len(selected) >= target_n:
            return selected, lo, hi
        widen += widen_step
    raise BandUnreachableError(
        f"borderline band unreachable: widened to [{lo:.2f},{hi:.2f}] "
        f"(cap +/-{max_widen}), only {len(selected)}/{target_n} problems selected "
        f"out of {len(scored)} screened. pass_rate histogram: {_pass_rate_histogram(scored)}"
    )


def select_borderline_band(scored: list[dict], target_n: int = 60, band_lo: float = 0.40,
                            band_hi: float = 0.60, widen_step: float = 0.10,
                            max_widen: float = 0.30) -> list[dict]:
    """Contract entrypoint (locked signature, 12-01): select the subset of
    `scored` ({task_id, pass_rate, ...}) rows in an adaptively-widened
    [band_lo,band_hi] band, until len(selected) >= target_n. Fails loud
    (BandUnreachableError) if target_n is unreachable at the widening cap.
    See _widen_to_target for the widening mechanics."""
    selected, _lo, _hi = _widen_to_target(scored, target_n, band_lo, band_hi,
                                          widen_step, max_widen)
    return selected


def _write_band(path: str, selected: list[dict], problems: list[dict]) -> None:
    """Write the selected borderline subset to `path`, one JSON row per
    problem: task_id + measured pass_rate + first_sample_temp (always None
    -- the screening ladder's first sample keeps generate()'s own default
    temperature) + the downstream fields (prompt/test/entry_point/kind) so
    ab_harness.py never needs to re-read humaneval.jsonl."""
    by_id = {p["task_id"]: p for p in problems}
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in selected:
            problem = by_id[row["task_id"]]
            out_row = {
                "task_id": row["task_id"],
                "pass_rate": row["pass_rate"],
                "first_sample_temp": None,
                "prompt": problem.get("prompt", ""),
                "test": problem.get("test", ""),
                "entry_point": problem.get("entry_point", ""),
                "kind": problem.get("kind", "function"),
            }
            f.write(json.dumps(out_row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--problems", default="data/evals/humaneval.jsonl")
    ap.add_argument("--dataset", default="humaneval")
    ap.add_argument("--out", default="data/evals/humaneval_band.jsonl")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--band-lo", type=float, default=0.40)
    ap.add_argument("--band-hi", type=float, default=0.60)
    ap.add_argument("--target-n", type=int, default=60)
    ap.add_argument("--widen-step", type=float, default=0.10)
    ap.add_argument("--max-widen", type=float, default=0.30)
    ap.add_argument("--model", default="vera-coder")
    ap.add_argument("--ollama-url", default=os.getenv("OLLAMA_OPENAI_URL", "http://127.0.0.1:11434/v1"))
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--gen-timeout", type=int, default=120)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip", type=int, default=0)
    args = ap.parse_args()

    problems = code_trainer.load_problems(args)
    t0 = time.time()
    scored = _score_all(problems, args)

    try:
        selected, lo, hi = _widen_to_target(scored, args.target_n, args.band_lo, args.band_hi,
                                            args.widen_step, args.max_widen)
    except BandUnreachableError as e:
        print(f"[!] {e}")
        return 1

    _write_band(args.out, selected, problems)
    print(f"selected {len(selected)}/{len(problems)} borderline problems "
          f"(band [{lo:.2f},{hi:.2f}]) -> {args.out} ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
