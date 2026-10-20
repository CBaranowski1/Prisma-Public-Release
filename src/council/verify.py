# src/council/verify.py
"""Execution-verification gate for the council — the anti-hallucination step.

A council answer that contains code is only trustworthy if the code actually
holds up. This module extracts the code from a candidate answer and checks it,
in two escalating tiers:

  1. SYNTAX (always, safe, deterministic, no execution): code that does not parse
     is a hallucination — flag it. Zero risk, so this tier is always on.
  2. EXECUTION (opt-in): run the snippet in an isolated subprocess with a hard
     timeout and capture any crash. Catches import errors, exceptions at
     definition/first-run, and obvious runtime breakage. Off by default because
     running generated code is a privileged action; the caller enables it
     explicitly (mirrors the app's gate-risky-features convention).

`verify_answer` returns a plain dict verdict the orchestrator turns into an
event and a `verified` flag on the final answer. Nothing here raises — a broken
verifier must never take down a deliberation.

SEC-08 (Phase 11): the gate used to only ever LOOK at Python. Any other fenced
language (SQL, bash, JS/TS/HTML, ...) silently fell through as if it had
never been checked at all -- `passed=True` regardless. That is a false-
assurance bug: "we did not verify this" was indistinguishable from "we
verified this and it's clean." This module now widens fence detection to
EVERY language tag, dispatches to an in-env validator where one exists
(Python via ast.parse/subprocess-exec, as before; bash/sh/shell via
`bash -n`; SQL via sqlglot IF importable -- see WR-04 note below), and
HONESTLY flags the rest as unverified via two new verdict keys:

  verified              -- True only when every detected block was genuinely
                            validated clean (or there was no code at all).
  unverified_languages  -- sorted list of fence tags detected but not
                            validated in this environment.

`passed` keeps its EXISTING, narrower meaning for backward compatibility:
False if and only if a VALIDATED block genuinely failed (syntax/exec/bash-
parse error). Unverified blocks NEVER flip `passed` to False -- this is
deliberate (D-02): the repair loop (`src/verified_gate.py`,
`needs_repair = not verdict.get("passed")`) must not hard-block an answer
just because it contains an illustrative JS/SQL snippet that this
environment has no validator for. Honesty lives in `verified` /
`unverified_languages`, not in `passed`.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
import os

# ```<lang> … ``` fenced blocks (lang may be empty). group(1) is the
# lowercased-at-use language tag, group(2) is the fenced body. Untagged ("")
# and {"python", "py"} tags take the EXACT existing ast.parse/exec path --
# byte-for-byte with pre-SEC-08 behavior (regression-anchored by
# tests/test_agent_loop_verify_gate.py::test_gate_silent_on_untagged_non_python_fence,
# which exercises this via Chat's own separate entry trigger, not this gate
# directly -- but the invariant matters here too).
_FENCE = re.compile(r"```([a-zA-Z0-9_+-]*)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

_PYTHON_TAGS = {"", "python", "py"}
_SQL_TAGS = {"sql"}
_BASH_TAGS = {"bash", "sh", "shell"}
_RUBY_TAGS = {"ruby", "rb"}
_NO_VALIDATOR_TAGS = {"js", "javascript", "jsx", "ts", "typescript", "tsx", "html"}

# Importable-guarded SQL parser selection (D-01: no new runtime deps -- only
# use it IF it already happens to be importable in this environment; never
# require/install it). Not importable in the reference dev environment as of
# Phase 11 (confirmed in 11-RESEARCH.md), so SQL honestly lands in
# unverified_languages there -- exactly the D-01-compliant outcome, not a bug.
#
# WR-04: `sqlglot` is the ONLY SQL parser this module treats as a genuine
# validator -- it raises `ParseError` on invalid SQL. `sqlparse` was
# previously probed too, but it is a non-validating tokenizer that returns
# tokens for almost any non-empty string (it does not raise on broken SQL),
# so treating its tokenization as validation reported invalid SQL as
# `verified=True` -- the exact false-assurance bug SEC-08 exists to
# eliminate, just relocated to the SQL tier. That branch has been dropped;
# `sqlparse` being installed no longer changes this module's behavior.
_sql_parser = None
_sqlglot_mod = None
try:  # pragma: no cover - exercised only when sqlglot happens to be installed
    import sqlglot as _sqlglot_mod  # type: ignore

    _sql_parser = "sqlglot"
except Exception:  # pragma: no cover - defensive, never raise on import probing
    pass


def extract_python_blocks(text: str) -> list[str]:
    """Return fenced code blocks that are plausibly Python (parse or look like it).

    Kept for backward compatibility (existing callers/tests may still import
    this by name) -- returns only the untagged/python/py blocks, exactly what
    it returned before SEC-08 widened `_FENCE`.
    """
    blocks = []
    for tag, body in _iter_fences(text):
        if tag not in _PYTHON_TAGS:
            continue
        code = body.strip("\n")
        if not code.strip():
            continue
        blocks.append(code)
    return blocks


def _iter_fences(text: str):
    """Yield (lowercased_tag, body) for every fenced block in `text`."""
    for m in _FENCE.finditer(text or ""):
        yield m.group(1).strip().lower(), m.group(2)


def syntax_check(code: str) -> tuple[bool, str]:
    """Deterministic parse check. Safe — never executes."""
    try:
        ast.parse(code)
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError: line {e.lineno}, col {e.offset}: {e.msg}"
    except Exception as e:  # pragma: no cover - defensive
        return False, f"{type(e).__name__}: {e}"


# SEC-09 (Phase 20): run_snippet used to inherit the FULL parent os.environ
# (API keys/secrets) and had no memory/CPU/fd/file-size cap -- only the
# wall-clock `timeout` bounded it. Turning on verify_execute/agent_verify_execute
# then ran arbitrary model-authored code as the app user with the app's own
# secrets in its environment. True network/FS isolation (container/nsjail) is
# explicitly deferred (D-01, see 20-RESEARCH.md) -- this is the lean mitigation:
# an env allowlist scrub (never env={}, PATH must always survive -- an empty
# env can break the interpreter's own dynamic-linker/DLL search path) plus
# POSIX-only resource.setrlimit caps via preexec_fn, degrading cleanly to
# timeout+env-scrub-only on Windows (preexec_fn is POSIX-only and raises
# ValueError if passed unconditionally on Windows -- the kwarg itself must be
# omitted there, never passed as None).
_ENV_ALLOWLIST = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "PATHEXT")


def _scrubbed_env() -> dict:
    """Allowlisted subset of the parent env -- never the app's secrets."""
    return {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}


def _rlimit_preexec_fn(cpu_seconds: int):
    """POSIX-only rlimit closure for subprocess.run's preexec_fn kwarg.

    Lazily imports `resource` (raises ImportError/ModuleNotFoundError on
    Windows, where this is never called -- see the sys.platform guard at the
    call site). Caps are a lean starting point (20-RESEARCH.md A2/A4),
    tunable if a legitimate snippet ever trips one.

    WR-02: RLIMIT_CPU derives from `cpu_seconds` (the caller's `run_snippet`
    `timeout`, plus a small grace margin at the call site) instead of a
    hardcoded constant -- a raised wall-clock timeout now raises the CPU
    ceiling too, so a legitimate CPU-bound snippet isn't killed by SIGXCPU
    and misreported as a verification failure at a stale budget. The other
    caps (RLIMIT_AS/RLIMIT_NOFILE/RLIMIT_FSIZE) are unrelated to timeout and
    stay fixed.
    """
    import resource

    def _apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024,) * 2)

    return _apply


def run_snippet(code: str, timeout: int = 8, stdin: str = "") -> tuple[bool, str]:
    """Execute a self-contained snippet in an isolated subprocess.

    Returns (ok, detail). ok is True only on exit 0. Any non-zero exit, timeout,
    or exception is a failure with the captured stderr tail. This is a smoke test
    — it catches code that crashes on import/definition/first run, not full
    correctness (there are no hidden tests in a general council prompt).

    SEC-09: the child's env is scrubbed to an allowlist (no inherited secrets)
    and, on POSIX, resource-limited (CPU/address-space/fd/file-size) via
    `preexec_fn`. On Windows this degrades to timeout+env-scrub only.
    """
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            run_kwargs = dict(
                input=stdin, capture_output=True, text=True, timeout=timeout,
                env=_scrubbed_env(),
            )
            if sys.platform != "win32":
                try:
                    run_kwargs["preexec_fn"] = _rlimit_preexec_fn(timeout + 1)
                except ImportError:
                    pass  # degrade to timeout+env-scrub only
            p = subprocess.run([sys.executable, path], **run_kwargs)
        except subprocess.TimeoutExpired:
            return False, f"timeout after {timeout}s"
        if p.returncode == 0:
            return True, ""
        err = (p.stderr or p.stdout or "").strip().splitlines()
        return False, (err[-1] if err else f"exit {p.returncode}")[:300]
    except Exception as e:  # pragma: no cover - defensive
        return False, f"runner error: {type(e).__name__}: {e}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def ruby_exec_check(code: str, timeout: int = 8) -> tuple[bool | None, str]:
    """Execute a Ruby snippet in an isolated subprocess -- the D-09 EXECUTION
    tier (heavier than a `ruby -c` syntax-only check). Mirrors `run_snippet`'s
    SEC-09 posture byte-for-byte: temp-file source, scrubbed env (no
    inherited app secrets), a hard wall-clock timeout, and POSIX-only
    resource caps via `preexec_fn` (degrades to timeout+env-scrub-only on
    Windows, where `preexec_fn` is unsupported).

    Returns (ok, detail):
      (True, "")                 -- the interpreter exited 0 (loaded/ran clean;
                                     this catches parse errors AND top-level
                                     runtime crashes, but never checks the
                                     RESULT of correctly-loading code -- there
                                     are no hidden tests in a general council
                                     prompt, D-02).
      (False, detail)            -- non-zero exit or timeout.
      (None, "ruby not available") -- the `ruby` interpreter isn't on PATH;
                                     callers must degrade this to honest
                                     unverified, never a hard failure.
    """
    fd, path = tempfile.mkstemp(suffix=".rb")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            run_kwargs = dict(
                capture_output=True, text=True, timeout=timeout,
                env=_scrubbed_env(),
            )
            if sys.platform != "win32":
                try:
                    run_kwargs["preexec_fn"] = _rlimit_preexec_fn(timeout + 1)
                except ImportError:
                    pass  # degrade to timeout+env-scrub only
            p = subprocess.run(["ruby", path], **run_kwargs)
        except FileNotFoundError:
            return None, "ruby not available"
        except subprocess.TimeoutExpired:
            return False, f"timeout after {timeout}s"
        if p.returncode == 0:
            return True, ""
        err = (p.stderr or p.stdout or "").strip().splitlines()
        return False, (err[-1] if err else f"exit {p.returncode}")[:300]
    except Exception as e:  # pragma: no cover - defensive
        return False, f"runner error: {type(e).__name__}: {e}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def bash_syntax_check(code: str, timeout: int = 8) -> tuple[bool | None, str]:
    """Syntax-only bash check via `bash -n` -- never executes the script.

    Follows the run_snippet subprocess+timeout idiom, but feeds the script
    via STDIN (`bash -n` with no file operand reads and syntax-checks
    standard input) rather than a temp-file path argument: a temp-file path
    is Windows-style (`C:\\Users\\...`) while `bash` on a Windows dev box may
    resolve to either Git Bash (`/c/Users/...`) or WSL bash (`/mnt/c/Users/...`)
    depending on PATH resolution order -- neither understands the other's
    path convention, so a path argument is fragile across environments.
    STDIN has no such ambiguity.
    Returns:
      (True, "")               -- bash -n exited 0 (clean parse)
      (False, detail)          -- bash -n found a genuine syntax error, or timed out
      (None, "bash not available") -- bash isn't on PATH in this environment;
                                       this is a graceful degrade to
                                       "unverified", never a hard failure.
    """
    try:
        p = subprocess.run(
            ["bash", "-n"], input=code, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        # bash not on PATH in this environment -- treat as unverified,
        # not a hard failure (never-raise, defensive-except convention).
        return None, "bash not available"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as e:  # pragma: no cover - defensive
        return False, f"runner error: {type(e).__name__}: {e}"
    if p.returncode == 0:
        return True, ""
    err = (p.stderr or "").strip().splitlines()
    return False, (err[-1] if err else f"exit {p.returncode}")[:300]


def sql_syntax_check(code: str) -> tuple[bool, str]:
    """Parse-only SQL syntax check -- never executes. Only callable when
    `_sql_parser == "sqlglot"` (the only genuinely VALIDATING SQL parser
    available); callers must gate on that before invoking this.

    WR-04: `sqlparse` is a non-validating tokenizer -- it does not raise on
    syntactically invalid SQL and returns tokens for almost any non-empty
    string, so treating its tokenization as validation would report broken
    SQL as `verified=True` (the exact false-assurance bug SEC-08 exists to
    eliminate, just relocated to the SQL tier). This function therefore never
    treats a `sqlparse`-only environment as validated; only `sqlglot`, which
    genuinely raises `ParseError` on invalid SQL, can produce a clean result.
    """
    try:
        if _sql_parser == "sqlglot":
            _sqlglot_mod.parse(code)
            return True, ""
        # sqlparse (non-validating) or no parser at all: never a genuine
        # validation, so this is honestly unverified -- callers must gate
        # on `_sql_parser == "sqlglot"` before invoking this function; this
        # branch exists as a defensive fallback, not a validation path.
        return False, "no genuine SQL validator available (sqlparse does not validate)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _classify(tag: str) -> str:
    """Bucket a fence tag into one of: 'python', 'bash', 'sql', 'ruby',
    'no_validator', or 'other' (an unrecognized tag -- treated the same as
    no_validator: honestly unverified, never blocking)."""
    if tag in _PYTHON_TAGS:
        return "python"
    if tag in _BASH_TAGS:
        return "bash"
    if tag in _SQL_TAGS:
        return "sql"
    if tag in _RUBY_TAGS:
        return "ruby"
    if tag in _NO_VALIDATOR_TAGS:
        return "no_validator"
    return "other"


def verify_answer(text: str, execute: bool = False, timeout: int = 8) -> dict:
    """Verify every fenced code block in a candidate answer, per-language.

    Verdict dict:
      has_code             – any fenced block found, any language
      syntax_ok            – all VALIDATED blocks parse/parse-check clean
      ran                  – Python execution tier was attempted
      passed               – overall pass, NON-BLOCKING for unverified code:
                              False iff a VALIDATED block genuinely failed
                              (syntax/exec/bash-parse error). Unverified
                              blocks (no in-env validator) never flip this
                              to False -- see D-02 in module docstring.
      verified             – NEW (SEC-08): True only when has_code is False,
                              or every detected block was genuinely
                              validated clean AND unverified_languages is
                              empty. This is the HONEST signal -- reporting
                              call sites that claim "verified" to a user
                              must read THIS key, not `passed`.
      unverified_languages – NEW (SEC-08): sorted unique list of fence tags
                              that were detected but had no in-env
                              validator to check them against.
      detail                – first genuine failure message, or "" on pass
      blocks                – total fenced-block count, across all languages
    A prose-only answer (no fenced code) returns has_code=False, passed=True,
    verified=True, unverified_languages=[] (nothing to falsify or flag).
    """
    fences = list(_iter_fences(text))
    # Drop empty/whitespace-only bodies -- mirrors the original
    # extract_python_blocks behavior (blank fences are not "code").
    fences = [(tag, body) for tag, body in fences if body.strip("\n").strip()]

    if not fences:
        return {"has_code": False, "syntax_ok": True, "ran": False,
                "passed": True, "verified": True, "unverified_languages": [],
                "detail": "", "blocks": 0}

    total_blocks = len(fences)
    unverified_tags: set[str] = set()
    python_blocks: list[str] = []
    ruby_blocks: list[str] = []

    # Tier 1: syntax/parse-only validation for every block that has a
    # validator; classify the rest as unverified. First genuine failure wins
    # (matches the original early-return-on-first-failure shape).
    for tag, body in fences:
        code = body.strip("\n")
        bucket = _classify(tag)

        if bucket == "python":
            ok, msg = syntax_check(code)
            if not ok:
                return {"has_code": True, "syntax_ok": False, "ran": False,
                        "passed": False, "verified": False, "unverified_languages": [],
                        "detail": msg, "blocks": total_blocks}
            python_blocks.append(code)
            continue

        if bucket == "bash":
            ok, msg = bash_syntax_check(code, timeout=timeout)
            if ok is None:
                # bash not available in this environment -- honest unverified,
                # not a hard failure.
                unverified_tags.add(tag)
                continue
            if not ok:
                return {"has_code": True, "syntax_ok": False, "ran": False,
                        "passed": False, "verified": False, "unverified_languages": [],
                        "detail": msg, "blocks": total_blocks}
            continue

        if bucket == "sql":
            if _sql_parser != "sqlglot":
                # Either no SQL parser is importable (D-01's expected,
                # honest outcome), or only `sqlparse` is available -- a
                # non-validating tokenizer that does not raise on invalid
                # SQL (WR-04). Only `sqlglot`'s genuine `ParseError` on
                # parse failure earns a validated result; everything else
                # is honestly unverified, non-blocking.
                unverified_tags.add(tag)
                continue
            ok, msg = sql_syntax_check(code)
            if not ok:
                return {"has_code": True, "syntax_ok": False, "ran": False,
                        "passed": False, "verified": False, "unverified_languages": [],
                        "detail": msg, "blocks": total_blocks}
            continue

        if bucket == "ruby":
            if not execute:
                # Ruby has no syntax-only in-env validator -- it can only be
                # genuinely checked by the (opt-in, privileged) execution
                # tier below. Without execute=True this is honestly
                # unverified, non-blocking -- unchanged from today.
                unverified_tags.add(tag)
                continue
            # execute=True: defer to the execution tier below -- do NOT mark
            # unverified yet, the block may still turn out genuinely clean.
            ruby_blocks.append(code)
            continue

        # bucket in {"no_validator", "other"}: detected, honestly unverified.
        unverified_tags.add(tag or "untagged")

    if not execute:
        verified = not unverified_tags
        return {"has_code": True, "syntax_ok": True, "ran": False,
                "passed": True, "verified": verified,
                "unverified_languages": sorted(unverified_tags),
                "detail": "", "blocks": total_blocks}

    # Execution tier (Python only, as before): run the largest self-contained-
    # looking block; skip pure snippets that only define a function with no call.
    for code in python_blocks:
        runnable = ("__main__" in code or "\nprint(" in code or code.strip().startswith("print(")
                    or "input(" in code or "sys.stdin" in code)
        if not runnable:
            continue
        ok, msg = run_snippet(code, timeout=timeout)
        if not ok:
            return {"has_code": True, "syntax_ok": True, "ran": True,
                    "passed": False, "verified": False, "unverified_languages": [],
                    "detail": msg, "blocks": total_blocks}

    # Ruby execution tier (D-09): unlike Python, Ruby has no separate
    # syntax-only pre-check -- a single execution catches both malformed
    # syntax AND top-level runtime crashes, so a failure here always
    # reports syntax_ok=False (the tier cannot distinguish the two -- both
    # are genuine, blocking verification failures).
    for code in ruby_blocks:
        ok, msg = ruby_exec_check(code, timeout=timeout)
        if ok is None:
            # ruby not available in this environment -- honest unverified,
            # not a hard failure.
            unverified_tags.add("ruby")
            continue
        if not ok:
            return {"has_code": True, "syntax_ok": False, "ran": True,
                    "passed": False, "verified": False, "unverified_languages": [],
                    "detail": msg, "blocks": total_blocks}

    verified = not unverified_tags
    return {"has_code": True, "syntax_ok": True, "ran": True,
            "passed": True, "verified": verified,
            "unverified_languages": sorted(unverified_tags),
            "detail": "", "blocks": total_blocks}
