"""One-shot polyglot eval orchestrator (robust; all logic in Python, not batch).

  python run_eval.py [model] [--backend ollama|openai|app|llamacpp|mock]
                     [--sql-backend postgres|sqlite] [--council] [--limit N]

Gates (abort with a clear message + nonzero exit — never a silent/fake number):
  1. grader self-check   2. runtime preflight   3. per-run result validation
"""
import argparse
import os
import shutil
# Fixed-argument local harness subprocesses only; shell execution is never used.
import subprocess  # nosec B404
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

def run(args, **kw):
    # Arguments are assembled internally as a list; shell execution is never used.
    return subprocess.run([PY] + args, cwd=HERE, **kw).returncode  # nosec B603

def die(msg, code=1):
    print("\n***** EVAL ABORTED: " + msg + " *****")
    sys.exit(code)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", default=os.getenv("MODEL", "qwen3-coder:30b"))
    ap.add_argument("--backend", default=os.getenv("BACKEND", "ollama"))
    ap.add_argument("--sql-backend", default=os.getenv("SQL_BACKEND", "postgres"))
    ap.add_argument("--council", action="store_true", default=os.getenv("RUN_COUNCIL") == "1")
    ap.add_argument("--limit", type=int, default=int(os.getenv("LIMIT", "0")))
    ap.add_argument("--langs", default=os.getenv("LANGS", "ruby,postgres"))
    ap.add_argument("--base-url", default=os.getenv("POLYGLOT_OPENAI_BASE_URL"),
                    help="OpenAI-compatible host, /v1 base, or full chat endpoint")
    ap.add_argument("--reasoning-effort", choices=["low", "medium", "high", "max"])
    ap.add_argument("--api-key-env", default="POLYGLOT_OPENAI_API_KEY",
                    help="name of the environment variable containing the API key")
    a = ap.parse_args()

    safe = a.model.replace(":", "_").replace("/", "_")
    res = os.path.join(HERE, "results"); os.makedirs(res, exist_ok=True)
    langs = [x.strip() for x in a.langs.split(",") if x.strip()]
    all_ds = {"ruby": os.path.join(HERE, "datasets", "ruby.jsonl"),
              "ruby_holdout": os.path.join(HERE, "datasets", "ruby_holdout.jsonl"),
              "postgres": os.path.join(HERE, "datasets", "postgres.jsonl")}
    ds = {k: v for k, v in all_ds.items() if k in langs}
    if not ds:
        die("no valid --langs selected (use ruby and/or postgres).")

    print("=" * 60)
    print(f"  POLYGLOT EVAL  model={a.model}  backend={a.backend}  sql={a.sql_backend}  council={a.council}")
    print("=" * 60)

    # gate 1: preflight (environment) FIRST so a missing runtime is a clean message
    print("\n[1/4] Preflight (runtimes + endpoint) ...")
    if a.backend == "ollama":
        if run(["preflight.py", "--model", a.model, "--sql-backend", a.sql_backend,
                "--langs", ",".join({("ruby" if l.startswith("ruby") else l) for l in langs}), "--app", "1" if a.council else "0"]) != 0:
            die("preflight failed — fix the message above and re-run.")
    else:
        if a.backend == "llamacpp" and not os.path.exists(a.model):
            die(f"model file not found: {a.model}")
        if any(l.startswith("ruby") for l in langs):
            try:
                ruby = shutil.which("ruby")
                if not ruby:
                    raise FileNotFoundError
                # The executable is resolved to an absolute path and argv is fixed.
                probe = subprocess.run(  # nosec B603
                    [ruby, "-e", "puts 2+2"], capture_output=True, text=True)
                if probe.returncode != 0 or probe.stdout.strip() != "4":
                    raise RuntimeError(probe.stderr.strip() or "unexpected Ruby output")
                print("[ok] ruby executes")
            except FileNotFoundError:
                die("ruby not found on PATH — install Ruby or run --langs postgres")
            except Exception as e:
                die(f"ruby not working: {e}")

    # gate 2: grader self-check (only the selected datasets)
    print("\n[2/4] Grader self-check ...")
    if run(["selfcheck.py"] + list(ds.values())) != 0:
        die("grader self-check failed — a pass rate would be untrustworthy.")

    # gate 3: baseline arms + validation
    outs = {}
    for i, (lang, path) in enumerate(ds.items(), start=1):
        print(f"\n[3/4] Baseline {lang} ...")
        out = os.path.join(res, f"{safe}_{lang}_baseline.jsonl")
        outs[lang] = out
        cmd = ["harness.py", "--dataset", path, "--backend", a.backend, "--model", a.model,
               "--arm", "baseline", "--out", out, "--sql-backend", a.sql_backend]
        if a.base_url:
            cmd += ["--base-url", a.base_url]
        if a.reasoning_effort:
            cmd += ["--reasoning-effort", a.reasoning_effort]
        cmd += ["--api-key-env", a.api_key_env]
        if a.limit: cmd += ["--limit", str(a.limit)]
        if run(cmd) != 0:
            die(f"{lang} baseline run crashed.")
        if run(["validate_results.py", "--results", out, "--dataset", path]) != 0:
            die(f"{lang} results failed validation (empty / wrong count / too many model errors).")

    # The architecture arm is NOT measurable through this runner: /api/v1/chat
    # is a bare model passthrough (no brain/agents), so an HTTP "council" arm
    # would measure nothing. The real architecture runs in-process via
    # run_council — use council_arm.py (needs the app venv + Ollama).
    if a.council:
        print("\n[4/4] Council arm is NOT run here.")
        print("      /api/v1/chat is a passthrough (no brain/agents). Run the REAL")
        print("      3-arm weak/strong/escalation A/B in-process from the repo root")
        print("      with the venv (Phase 41 controlled reasoning/architecture arms):")
        for lang, path in ds.items():
            strong_model = a.model if a.backend == "openai" else "qwen3-coder:30b"
            command = (
                ".\\venv\\Scripts\\python.exe scripts\\polyglot_ab\\council_arm.py "
                f"--dataset scripts\\polyglot_ab\\datasets\\{lang}.jsonl "
                f"--backend {a.backend} --weak-model {a.model} "
                f"--strong-model {strong_model} "
                "--weak-reasoning low --strong-reasoning max "
                f"--architecture-reasoning low --sql-backend {a.sql_backend}"
            )
            if a.base_url:
                command += f" --base-url {a.base_url}"
            print(f"        {command}")

    print("\n" + "=" * 60 + f"\n  PROFICIENCY SUMMARY  ({a.model})\n" + "=" * 60)
    for lang, out in outs.items():
        run(["validate_results.py", "--results", out, "--dataset", ds[lang]])
    print(f"\nResults in: {res}\nDONE.")

if __name__ == "__main__":
    main()
