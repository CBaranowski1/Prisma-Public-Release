# Polyglot Proficiency / A-B Harness — Ruby + Postgres

Benchmark a model's Ruby and PostgreSQL proficiency by real execution, and A/B
the bare model against the app's architecture (agents + second brain).

Built to analyze **qwen3-coder:30b**; validate the pipeline first with
**llama3.1:8b**. Both run on YOUR machine (they need your GPU + Ollama); this
folder was authored and self-checked here with real `ruby` + SQL execution, and
smoke-tested live with Llama-3.2-1B.

## One-shot runner (Windows) — `run_eval.bat`

Runs the whole thing with fail-loud gates so a bad run can't report a fake number:

```bat
run_eval.bat                 :: qwen3-coder:30b proficiency (Ruby + Postgres)
run_eval.bat llama3.1:8b     :: validate the pipeline on the 8B first
set RUN_COUNCIL=1 && run_eval.bat   :: also run the architecture A/B (needs the app + PRISM_API_TOKEN)
```

It aborts (non-zero exit, nothing trusted) if any gate fails:
1. **Preflight** — Ollama up, the model actually pulled, a probe generate returns, `ruby` runs, Postgres answers `SELECT 1`.
2. **Grader self-check** — every reference solution passes and every wrong answer fails (if not, the grader is broken and no pass rate is trustworthy).
3. **Per-run validation** — row count matches the dataset, and it refuses to print a proficiency number if >25% of model calls errored (a dead endpoint can't masquerade as "low skill").

Anti-hiccup env: `SQL_BACKEND=sqlite` (skip Postgres, portable only), `OLLAMA_HOST`, `POLYGLOT_PG_DSN`, `PRISM_URL`, `PRISM_API_TOKEN`.

## Layout
- `datasets/ruby.jsonl`, `datasets/postgres.jsonl` — problems with reference solution + tests.
- `runners.py` — execution scorers (ruby subprocess; SQL via psycopg→Postgres, or stdlib sqlite).
- `harness.py` — asks a model per problem, scores, writes `{task_id,language,passed,arm}` JSONL.
- `selfcheck.py` — proves every reference passes and every wrong answer fails (no model needed).

## Prereqs (your machine)
```bash
ollama pull llama3.1:8b
ollama pull qwen3-coder:30b
ruby --version                      # any ruby 3.x
pip install "psycopg[binary]"       # Postgres client for SQL scoring
createdb polyglot_ab                # a throwaway Postgres db; fixtures are rolled back each problem
export POLYGLOT_PG_DSN=postgresql:///polyglot_ab   # or postgresql://user:pw@host/db
```

## 0. Self-check the benchmark (no model, sqlite backend)
```bash
python selfcheck.py          # expect: all references PASS, all wrong answers FAIL
```

## 1. Proficiency — qwen3-coder:30b (baseline: raw model, single call)
```bash
mkdir -p results
# Ruby (scored by real ruby)
python harness.py --dataset datasets/ruby.jsonl --backend ollama \
  --model qwen3-coder:30b --arm baseline --out results/qwen_ruby.jsonl
# Postgres (scored against your live Postgres via POLYGLOT_PG_DSN)
python harness.py --dataset datasets/postgres.jsonl --backend ollama \
  --model qwen3-coder:30b --arm baseline --out results/qwen_pg.jsonl --sql-backend postgres
```
Each run prints per-language `pass@1` — that's the proficiency number.
Swap `--model llama3.1:8b` to validate the pipeline on the 8B first.

## 2. A/B — architecture (agents + second brain) vs bare model
Start the app (so `/api/v1/chat` runs the council), then:
```bash
# baseline arm = raw model
python harness.py --dataset datasets/ruby.jsonl --backend ollama --model qwen3-coder:30b \
  --arm baseline --out results/ruby_baseline.jsonl
# council arm = through the app (brain + agents)
PRISM_URL=http://localhost:7000 PRISM_API_TOKEN=<chat-scoped token> \
python harness.py --dataset datasets/ruby.jsonl --backend app --model qwen3-coder:30b \
  --arm council --out results/ruby_council.jsonl
# McNemar A/B (reuses the repo's existing report script)
python ../ab_report.py --baseline results/ruby_baseline.jsonl --withbrain results/ruby_council.jsonl
```
(`ab_report.py` reads `{task_id,passed}`, which this harness writes — directly compatible.)

## Saturation
Python saturates qwen3-coder:30b; Ruby/Postgres have more headroom. If the 30B
still aces a set, add harder problems (same JSONL format) or screen to the
borderline band: run the baseline once and keep only the problems it gets
~20–80% right, then A/B on that subset — same approach as the repo's
`screen_borderline.py`.

## Notes
- `--sql-backend sqlite` runs SQL on stdlib sqlite (portable; used for self-check). Use
  `--sql-backend postgres` (default) for a true Postgres proficiency read.
- Runs are resumable (append + skip already-scored task_ids).
- If your app's `/api/v1/chat` returns a different JSON field, adjust `call_app()` in `harness.py`.
