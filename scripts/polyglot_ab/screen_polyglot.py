"""Dual-model borderline screener for the Ruby/Postgres band (RMEAS-01,
41-03). Adapts scripts/screen_borderline.py's PROVEN adaptive-widening
ALGORITHM shape (_widen_to_target / BandUnreachableError / a pass_rate
histogram diagnostic) to real Ruby/SQL execution against BOTH production
models -- NOT imported from screen_borderline.py, which is coupled to
code_trainer.generate/run_tests/load_problems (Python/HumanEval-specific).

For each problem, screens BOTH configured inference arms. The current Spark
measurement pins both arms to GLM 5.3 Flash and varies reasoning effort
(low versus max); the legacy Ollama two-model configuration remains supported.

and selects the problem only if weak_rate falls in the borderline band
[band_lo, band_hi] AND strong_rate stays below saturation_ceiling -- i.e.
genuine headroom for BOTH models (RMEAS-01), not just the weak one (the
single-model criterion scripts/screen_borderline.py used for Phase 12).

Reuses (by module reference, never re-implemented):
  - scripts/polyglot_ab/harness.py's PROMPT_TMPL + call_model for the
    per-sample generation call (the SAME Ruby/Postgres-aware prompt the
    live 3-arm run uses).
  - scripts/polyglot_ab/runners.py's score() for real Ruby/SQL execution
    scoring (sql_backend="sqlite" default per D-02 -- no local Postgres
    server present).

Two pitfalls this file deliberately avoids (41-RESEARCH.md, inherited from
.planning/phases/12-effectiveness-measurement-a-b/12-RESEARCH.md):

  - Pitfall 4: a best-of-N sampler (code_trainer.sample_solutions) short-
    circuits on the FIRST passing sample -- it cannot produce a pass RATE.
    screen_pass_rate below runs ALL k samples every time, exactly as
    screen_borderline.screen_pass_rate does.
  - Pitfall 8: screening at a single low, near-deterministic temperature
    clusters nearly every problem at pass_rate 0%/100%, leaving the
    borderline band almost empty. _temperature_ladder below supplies a
    diversified discovery ladder, decoupled from the scored arms' own
    pinned low temperature (a separate concern, not touched by this file).
    It deliberately does NOT reuse code_trainer._sample_temps verbatim --
    that ladder's first slot is `None` (meaning "keep generate()'s own
    configured default"), a convention specific to code_trainer.generate.
    the model transport requires a concrete float
    on every call, so this file inlines an equivalent-shaped ladder (same
    "low anchor + spread 0.5-0.8" diversity) that is always numeric.

harness/runners are imported as MODULE OBJECTS (`import harness`, `import
runners`), never via `from harness import call_ollama` / `from runners
import score` -- a name-bound import would freeze the function object at
import time, defeating any caller (test or otherwise) that patches
`harness.call_ollama` / `runners.score` on the module itself (the same
name-binding trap 12-RESEARCH.md documents for seat_runner.
untrusted_context_message, and screen_borderline.py's own docstring
documents for code_trainer).

Live model calls happen ONLY under main() / `if __name__ == "__main__"` --
importing this module makes no live call.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))   # scripts/polyglot_ab
_REPO = os.path.dirname(os.path.dirname(_HERE))       # scripts/polyglot_ab -> scripts -> repo root
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness  # noqa: E402  -- polyglot prompt + backend-neutral model call
import runners  # noqa: E402  -- scripts/polyglot_ab/runners.py (score)


class BandUnreachableError(RuntimeError):
    """Raised when the polyglot dual-model band cannot reach target_n even
    at the widening cap -- a genuinely too-easy/too-hard model pairing
    surfaced, not hidden behind a silently-tiny n."""


def _temperature_ladder(k: int) -> list[float]:
    """Temperature-diversity discovery ladder (Pitfall 8): sample 1 at a
    low baseline temperature, later samples spread 0.5-0.8 for diversity.
    Always returns concrete floats (never None)."""
    if k <= 1:
        return [0.2]
    return [0.2] + [round(0.5 + 0.3 * i / max(1, k - 2), 2) for i in range(k - 1)]


def screen_pass_rate(problem: dict, k: int, model: str,
                     temperature_ladder: list[float] | None = None,
                     sql_backend: str = "sqlite", *,
                     backend: str = "ollama", base_url: str | None = None,
                     reasoning_effort: str | None = None,
                     api_key_env: str = harness.OPENAI_API_KEY_ENV) -> float:
    """Run k temperature-diverse samples of `model` against `problem` (a
    Ruby or Postgres/SQL polyglot_ab problem dict) and score EACH one via
    runners.score. Never short-circuits on the first pass (Pitfall 4). A
    flaky per-sample exception (model call OR scoring) degrades that
    sample to a fail rather than aborting the whole screen. Returns the
    measured pass_rate = passes/k."""
    ladder = temperature_ladder if temperature_ladder is not None else _temperature_ladder(k)
    passes = 0
    for temp in ladder:
        try:
            prompt = harness.PROMPT_TMPL[problem["language"]].format(p=problem["prompt"])
            text = harness.call_model(
                backend, model, prompt, temp, base_url=base_url,
                reasoning_effort=reasoning_effort, api_key_env=api_key_env,
            )
            ok, _detail = runners.score(text, problem, sql_backend=sql_backend)
        except Exception:
            ok = False
        if ok:
            passes += 1
    return passes / k


def _load_cache(cache_path: str | None,
                expected_fingerprint: str | None = None) -> dict[str, dict]:
    """Load an incremental `_score_all` cache (one JSON row per line:
    task_id/weak_rate/strong_rate) into a task_id-keyed dict. Missing or
    absent cache_path returns an empty dict (nothing to resume)."""
    cache: dict[str, dict] = {}
    if not cache_path or not os.path.exists(cache_path):
        return cache
    with open(cache_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if (expected_fingerprint is None
                    or row.get("screen_fingerprint") == expected_fingerprint):
                cache[row["task_id"]] = row
    return cache


def _screen_fingerprint(*, backend: str, weak_model: str, strong_model: str,
                        weak_reasoning: str | None, strong_reasoning: str | None,
                        k: int, sql_backend: str, base_url: str | None) -> str | None:
    """Return a secret-free cache identity for non-legacy screening runs.

    The untouched Ollama defaults retain compatibility with Phase 41's
    existing cache rows. Any OpenAI/GLM or explicit reasoning run requires a
    matching fingerprint, so Qwen/Ollama rows can never be reused silently.
    """
    legacy = (backend == "ollama" and base_url is None
              and weak_reasoning is None and strong_reasoning is None)
    if legacy:
        return None
    endpoint = ""
    if backend == "openai":
        configured = base_url or os.getenv(
            harness.OPENAI_BASE_URL_ENV, "http://127.0.0.1:1919/v1")
        endpoint = harness.openai_chat_url(configured)
    identity = {
        "backend": backend,
        "endpoint": endpoint,
        "k": k,
        "sql_backend": sql_backend,
        "strong_model": strong_model,
        "strong_reasoning": strong_reasoning or "",
        "weak_model": weak_model,
        "weak_reasoning": weak_reasoning or "",
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _score_all(problems: list[dict], weak_model: str, strong_model: str, k: int,
               sql_backend: str = "sqlite", cache_path: str | None = None, *,
               backend: str = "ollama", base_url: str | None = None,
               weak_reasoning: str | None = None,
               strong_reasoning: str | None = None,
               api_key_env: str = harness.OPENAI_API_KEY_ENV) -> list[dict]:
    """Screen every problem's weak_rate + strong_rate once; keep just
    task_id + rates so the adaptive widening step can re-select from the
    ALREADY-COMPUTED rates without re-screening (mirrors screen_borderline.
    py's _score_all split).

    Resumable (41-07 hardening): if `cache_path` is given and already
    contains a row for a problem's task_id, that row is REUSED verbatim --
    screen_pass_rate (the expensive live model call) is never re-invoked for
    it. Every freshly-scored problem is appended to `cache_path` immediately
    (open/write/flush/close per row) so a kill mid-run loses at most the
    in-flight problem, never prior progress. Prints one flushed progress
    line per problem so a multi-hour run stays monitorable."""
    fingerprint = _screen_fingerprint(
        backend=backend, weak_model=weak_model, strong_model=strong_model,
        weak_reasoning=weak_reasoning, strong_reasoning=strong_reasoning,
        k=k, sql_backend=sql_backend, base_url=base_url,
    )
    cache = _load_cache(cache_path, fingerprint)
    scored = []
    n = len(problems)
    for i, problem in enumerate(problems, start=1):
        task_id = problem["task_id"]
        lang = problem.get("language", "?")
        if task_id in cache:
            row = cache[task_id]
            weak_rate, strong_rate, reused = row["weak_rate"], row["strong_rate"], True
        else:
            if fingerprint is None:
                weak_rate = screen_pass_rate(
                    problem, k, weak_model, sql_backend=sql_backend)
                strong_rate = screen_pass_rate(
                    problem, k, strong_model, sql_backend=sql_backend)
            else:
                call_options = {
                    "sql_backend": sql_backend,
                    "backend": backend,
                    "base_url": base_url,
                    "api_key_env": api_key_env,
                }
                weak_rate = screen_pass_rate(
                    problem, k, weak_model, reasoning_effort=weak_reasoning,
                    **call_options,
                )
                strong_rate = screen_pass_rate(
                    problem, k, strong_model, reasoning_effort=strong_reasoning,
                    **call_options,
                )
            reused = False
            if cache_path:
                cache_row = {"task_id": task_id, "weak_rate": weak_rate,
                             "strong_rate": strong_rate}
                if fingerprint is not None:
                    cache_row.update({
                        "backend": backend,
                        "weak_model": weak_model,
                        "strong_model": strong_model,
                        "weak_reasoning": weak_reasoning,
                        "strong_reasoning": strong_reasoning,
                        "screen_fingerprint": fingerprint,
                    })
                with open(cache_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(cache_row) + "\n")
                    f.flush()
        scored.append({"task_id": task_id, "weak_rate": weak_rate, "strong_rate": strong_rate})
        print(f"[{i}/{n}] {task_id} ({lang}) weak={weak_rate:.2f} strong={strong_rate:.2f}"
              f"{' (cached)' if reused else ''}", flush=True)
    return scored


def _pass_rate_histogram(scored: list[dict], key: str, buckets: int = 10) -> dict[str, int]:
    """Coarse pass_rate histogram (of `key`) for the fail-loud diagnostic."""
    hist: dict[str, int] = {}
    for row in scored:
        b = min(buckets - 1, int(row[key] * buckets))
        bucket_key = f"{b / buckets:.1f}-{(b + 1) / buckets:.1f}"
        hist[bucket_key] = hist.get(bucket_key, 0) + 1
    return hist


def _widen_to_target(scored: list[dict], target_n: int, band_lo: float, band_hi: float,
                      saturation_ceiling: float, widen_step: float, max_widen: float
                      ) -> tuple[list[dict], float, float]:
    """Widen [band_lo,band_hi] symmetrically in widen_step increments (up
    to max_widen), re-selecting from the already-computed `scored` rates
    each time, until len(selected) >= target_n. The strong-model saturation
    ceiling is a HARD CUTOFF -- never widened (headroom for the strong
    model is exactly what RMEAS-01 requires; widening it away would defeat
    the whole dual-model criterion). Returns (selected, lo, hi). Raises
    BandUnreachableError (fail loud) if target_n is unreachable even at the
    widening cap."""
    widen = 0.0
    while widen <= max_widen + 1e-9:
        lo, hi = max(0.0, band_lo - widen), min(1.0, band_hi + widen)
        selected = [row for row in scored
                    if lo <= row["weak_rate"] <= hi and row["strong_rate"] < saturation_ceiling]
        if len(selected) >= target_n:
            return selected, lo, hi
        widen += widen_step
    raise BandUnreachableError(
        f"polyglot borderline band unreachable: widened WEAK band to [{lo:.2f},{hi:.2f}] "
        f"(cap +/-{max_widen}), saturation_ceiling={saturation_ceiling} fixed (never widened), "
        f"only {len(selected)}/{target_n} problems selected out of {len(scored)} screened. "
        f"weak_rate histogram: {_pass_rate_histogram(scored, 'weak_rate')}; "
        f"strong_rate histogram: {_pass_rate_histogram(scored, 'strong_rate')}"
    )


def select_polyglot_band(scored: list[dict], target_n: int = 60, band_lo: float = 0.20,
                          band_hi: float = 0.80, saturation_ceiling: float = 0.9,
                          widen_step: float = 0.10, max_widen: float = 0.20) -> list[dict]:
    """Contract entrypoint: select the subset of `scored` ({task_id,
    weak_rate, strong_rate, ...}) rows whose weak_rate sits in an
    adaptively-widened [band_lo,band_hi] band AND whose strong_rate stays
    strictly below saturation_ceiling (headroom for BOTH models --
    RMEAS-01), until len(selected) >= target_n. Fails loud
    (BandUnreachableError) if target_n is unreachable at the widening cap.
    Pure over already-computed rows (mirrors screen_borderline.
    select_borderline_band's split between _score_all and _widen_to_target)
    so tests can drive it without any live model call. See
    _widen_to_target for the widening mechanics."""
    selected, _lo, _hi = _widen_to_target(scored, target_n, band_lo, band_hi,
                                          saturation_ceiling, widen_step, max_widen)
    return selected


def _write_band(path: str, selected: list[dict], problems: list[dict]) -> None:
    """Write the selected dual-headroom subset to `path`, one JSON row per
    problem: the full downstream problem fields (language/prompt/test/
    entry_point/schema/reference/... -- everything runners.score/council
    need) plus the measured weak_rate/strong_rate, so the live 3-arm run
    never needs to re-read the raw datasets."""
    by_id = {p["task_id"]: p for p in problems}
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in selected:
            problem = by_id[row["task_id"]]
            out_row = {**problem, "weak_rate": row["weak_rate"], "strong_rate": row["strong_rate"]}
            f.write(json.dumps(out_row) + "\n")


def _load_datasets(paths: list[str]) -> list[dict]:
    problems = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            problems.extend(json.loads(line) for line in f if line.strip())
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", "--datasets", dest="datasets", nargs="+",
                    default=[os.path.join(_HERE, "datasets", "ruby.jsonl"),
                             os.path.join(_HERE, "datasets", "postgres.jsonl")],
                    help="one or more polyglot_ab dataset JSONL paths")
    ap.add_argument("--weak-model", default="vera-coder")
    ap.add_argument("--strong-model", default="qwen3-coder:30b")
    ap.add_argument("--backend", default="ollama",
                    choices=["ollama", "openai", "app", "llamacpp", "mock"])
    ap.add_argument("--base-url",
                    help="OpenAI-compatible host, /v1 base, or full chat endpoint")
    ap.add_argument("--weak-reasoning", choices=harness.REASONING_EFFORTS)
    ap.add_argument("--strong-reasoning", choices=harness.REASONING_EFFORTS)
    ap.add_argument("--api-key-env", default=harness.OPENAI_API_KEY_ENV,
                    help="name of the environment variable containing the API key")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--band-lo", type=float, default=0.20)
    ap.add_argument("--band-hi", type=float, default=0.80)
    ap.add_argument("--saturation-ceiling", type=float, default=0.9)
    ap.add_argument("--target-n", type=int, default=60)
    ap.add_argument("--widen-step", type=float, default=0.10)
    ap.add_argument("--max-widen", type=float, default=0.20)
    ap.add_argument("--sql-backend", default="sqlite", choices=["postgres", "sqlite"])
    ap.add_argument("--out", default=os.path.join(_HERE, "datasets", "polyglot_band.jsonl"))
    ap.add_argument("--cache", default=None,
                    help="incremental scoring cache path (resume on kill/restart); "
                         "default: alongside --out")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore/clear any existing --cache and re-score everything")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.cache is None:
        args.cache = os.path.join(os.path.dirname(args.out) or ".", "polyglot_screen_cache.jsonl")
    if args.fresh and os.path.exists(args.cache):
        os.remove(args.cache)

    problems = _load_datasets(args.datasets)
    if args.limit:
        problems = problems[:args.limit]

    fingerprint = _screen_fingerprint(
        backend=args.backend, weak_model=args.weak_model,
        strong_model=args.strong_model, weak_reasoning=args.weak_reasoning,
        strong_reasoning=args.strong_reasoning, k=args.k,
        sql_backend=args.sql_backend, base_url=args.base_url,
    )
    reused_before = _load_cache(args.cache, fingerprint)
    reused_n = sum(1 for p in problems if p["task_id"] in reused_before)

    t0 = time.time()
    scored = _score_all(problems, args.weak_model, args.strong_model, args.k,
                        sql_backend=args.sql_backend, cache_path=args.cache,
                        backend=args.backend, base_url=args.base_url,
                        weak_reasoning=args.weak_reasoning,
                        strong_reasoning=args.strong_reasoning,
                        api_key_env=args.api_key_env)
    print(f"resume summary: {reused_n}/{len(problems)} reused from cache, "
          f"{len(problems) - reused_n}/{len(problems)} newly scored ({args.cache})")

    try:
        selected, lo, hi = _widen_to_target(scored, args.target_n, args.band_lo, args.band_hi,
                                            args.saturation_ceiling, args.widen_step, args.max_widen)
    except BandUnreachableError as e:
        print(f"[!] {e}")
        return 1

    _write_band(args.out, selected, problems)
    print(f"selected {len(selected)}/{len(problems)} dual-headroom problems "
          f"(weak band [{lo:.2f},{hi:.2f}], strong ceiling <{args.saturation_ceiling}) "
          f"-> {args.out} ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
