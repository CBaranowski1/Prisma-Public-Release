# prism-UI — Improvement Backlog (beyond v3 close-out)

Prioritized, grounded improvements not already in the v3.0 close-out prompt. Each is
a candidate GSD milestone/phase. Priority = impact × leverage: **P0** protect what
exists / cheap-and-high-value; **P1** major value; **P2** worthwhile, larger or
lower-leverage. Every item states *why* and a one-line *done* test.

---

## North star — raise the offline-model ceiling
The v2.0 A/B proved the architecture lifts a model **with headroom** (llama-8b:
64.1%→85.9%, p=0.009) but the strong models **saturate** HumanEval, so scaffolding's
measurable gain is capped there. The remaining levers change the *model*, not the
prompt.

- **[P0] Continuous-eval guardrail.** Wire the proven `ab_harness.py` into a scheduled
  / pre-merge run so any brain, council, skills, or prompt change is scored against a
  fixed golden band — quality can't silently regress. *Cheapest high-value item on this
  list now that the harness exists.* Done: a CI/scheduled job fails if pass@1 drops
  beyond a threshold vs the committed baseline.
- **[P1] LoRA fine-tune on the verified-solution corpus.** The only lever that raises
  the *weights* ceiling for the strong models. Distill the 26k verified cc_solutions +
  technique notes into a LoRA for vera-coder; measure with the existing harness. Done:
  a fine-tuned adapter beats base vera-coder on a held-out band with McNemar p<0.05.
- **[P2] Task-aware model router.** Replace fixed escalation with a difficulty
  estimate (heuristic → learned) that picks small vs 30B per request. Done: router
  matches or beats always-30B pass@1 at lower median latency/tokens.

## Reliability & operability — protect the user's data and trust
- **[P1] Backup / restore.** The app holds irreplaceable user state (SQLite, memory
  vectors, `C:\path\to\vault`) with **no backup story**. Add an export/import + scheduled
  snapshot. Done: one command backs up and restores a full working instance.
- **[P1] Real DB migrations.** `core/database.py` uses a hand-rolled `_migrate_*`
  chain run at startup — brittle and unversioned. Adopt Alembic (or a versioned
  migration table). Done: schema changes apply via versioned migrations, not ad-hoc
  `ALTER` probes.
- **[P1] First-run guided setup + preflight.** CONCERNS flags silent Ollama/vLLM/model
  failures on fresh installs. Add a preflight (model reachable? brain indexed?
  fastembed present?) with actionable errors. Done: a clean machine reaches a working
  chat via guided checks, no cryptic failures.
- **[P2] Observability view.** No surface shows what the agents/brain actually do.
  Add a local metrics/trace panel: per-request latency, tokens, recall hits,
  verify-gate catches, model-routing decisions. Done: the user can see and tune the
  pipeline from the UI.

## Security — the biggest remaining gap
- **[P1] True shell/FS execution sandbox.** CONCERNS #1. v2 added env-scrub + rlimit
  (SEC-09) but explicitly deferred real isolation — agent `bash`/exec still runs as the
  app user with FS/network reach. Add container/nsjail/seccomp confinement for the
  execution + verify-gate paths. Done: a hostile snippet cannot read secrets, write
  outside a jail, or reach internal services.
- **[P2] Supply-chain: pinned lockfile + `pip-audit` in CI.** requirements are loosely
  pinned; no vuln scan. Done: CI fails on a known-vuln dependency; builds are
  reproducible from a lockfile.

## Performance — CONCERNS.md tail (helps small models most → ties to north star)
- **[P1] Agent prompt/context bloat.** Full tool schemas + skills + memory load every
  turn, exhausting 4k/8k-window models before the user's request. Add selective tool
  loading + early token-budget drop. Done: agent-mode input tokens drop materially with
  no task-success regression on the eval band.
- **[P2] Email latency profiling + connection pooling** (`routes/email_routes.py` is
  3.2k lines, no profiling). Done: measured round-trip improvement on a real IMAP/SMTP
  account.
- **[P2] Large-output streaming** instead of the hard `MAX_OUTPUT_CHARS`/`MAX_READ_CHARS`
  truncation that silently clips agent context. Done: large tool outputs paginate/stream,
  no silent loss.

## Maintainability & CI
- **[P1] Gate CI on tests + lint.** Once v3 drives the red floor to zero, flip
  `continue-on-error` off and add ruff (+ optionally mypy) as required checks. Done:
  a red test or lint error blocks merge.
- **[P2] Decompose the giant modules.** `tool_implementations.py` (4.1k),
  `email_routes.py` (3.2k), `agent_loop.py` (2.5k) are hard to test in isolation. Split
  into module-level, individually-testable units. Done: each split unit has direct tests.

## Data / knowledge hygiene
- **[P2] Brain de-ambiguation + staleness pruning.** The brain audit found ~2,370
  note-ids shared across categories (ambiguous wiki-link resolution) and no staleness
  policy. Rename generic ids; prune/mark stale notes. Done: 0 ambiguous ids; a
  documented pruning pass.
- **[P2] Frontend/CSS debt.** CONCERNS notes `static/style.css` is overgrown and
  modal/mobile rules are fragile. Dedicated cleanup + component patterns. Done: dead
  selectors removed; mobile layout verified.

---

## Suggested sequencing
1. **Right after v3 seals the baseline:** [P0] continuous-eval guardrail + [P1] gate CI
   — lock in quality so nothing regresses.
2. **v4 "operability" milestone:** backup/restore, DB migrations, first-run preflight,
   observability.
3. **v4/v5 "true isolation" milestone:** the execution sandbox (largest security item).
4. **Parallel research track:** the LoRA fine-tune — the real ceiling-raiser for the
   strong models, measured against the harness that already exists.

Nothing here is required for correctness today; it's the map from "working and proven"
to "durable, observable, and genuinely hard to regress."
