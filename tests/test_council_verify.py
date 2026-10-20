# tests/test_council_verify.py
"""Unit coverage for src/council/verify.py's widened, per-language verify
gate (SEC-08, Phase 11).

Targets `src.council.verify.verify_answer` (and `bash_syntax_check`) BY NAME
-- mirrors the sibling style in tests/test_agent_loop_verify_gate.py, which
exercises verify.py indirectly through Chat's own gate trigger. This file
exercises verify.py directly.

No bare `import sqlparse`/`import sqlglot`/`import chromadb` at module scope
-- neither SQL parser is importable in the reference dev environment
(11-RESEARCH.md), so the SQL assertions below are written to be correct in
EITHER environment: gated on the module's own `_sql_parser` import-guard
result, never assumed.
"""
from __future__ import annotations

import shutil
import sys

import pytest

import src.council.verify as verify


# ---------------------------------------------------------------------------
# Detection: sql/js/ts/html each land in unverified_languages, non-blocking.
# ---------------------------------------------------------------------------

class TestUnverifiedLanguageDetection:
    def test_js_block_is_detected_and_honestly_unverified(self):
        r = verify.verify_answer("```js\nvar x = 1;\n```")
        assert r["has_code"] is True
        assert r["passed"] is True
        assert r["verified"] is False
        assert r["unverified_languages"] == ["js"]

    def test_ts_block_is_detected_and_honestly_unverified(self):
        r = verify.verify_answer("```ts\nconst x: number = 1;\n```")
        assert r["passed"] is True
        assert r["verified"] is False
        assert r["unverified_languages"] == ["ts"]

    def test_html_block_is_detected_and_honestly_unverified(self):
        r = verify.verify_answer("```html\n<div>hi</div>\n```")
        assert r["passed"] is True
        assert r["verified"] is False
        assert r["unverified_languages"] == ["html"]

    def test_sql_block_honesty_gated_on_env_parser_availability(self):
        # WR-04: `sqlglot` is the ONLY SQL parser this module treats as a
        # genuine validator (it raises ParseError on invalid SQL); `sqlparse`
        # is a non-validating tokenizer and is never used to flip `verified`
        # to True, even if installed. If sqlglot is NOT importable (the
        # documented, expected Phase-11 outcome per 11-RESEARCH.md), a
        # syntactically-valid SELECT lands in unverified_languages,
        # non-blocking. If sqlglot IS importable, a valid SELECT should
        # verify clean instead -- either way `passed` stays True.
        r = verify.verify_answer("```sql\nSELECT 1\n```")
        assert r["passed"] is True
        if verify._sql_parser != "sqlglot":
            assert r["verified"] is False
            assert r["unverified_languages"] == ["sql"]
        else:
            assert r["verified"] is True
            assert r["unverified_languages"] == []

    def test_sqlparse_alone_never_validates_sql_even_if_installed(self, monkeypatch):
        # WR-04 regression guard: simulate an environment where sqlparse
        # (non-validating tokenizer) is the only "SQL parser" signal present.
        # Even a malformed statement that sqlparse would happily tokenize
        # must NOT be reported as verified -- sql_syntax_check must not be
        # dispatched to at all when `_sql_parser != "sqlglot"`.
        monkeypatch.setattr(verify, "_sql_parser", "sqlparse")
        r = verify.verify_answer("```sql\nthis is not ; ; valid <<< sql\n```")
        assert r["passed"] is True  # unverified is non-blocking
        assert r["verified"] is False
        assert r["unverified_languages"] == ["sql"]

    def test_sql_syntax_check_reports_unverified_when_not_sqlglot(self, monkeypatch):
        # Direct unit coverage: sql_syntax_check itself must never claim a
        # clean result unless `_sql_parser == "sqlglot"`.
        monkeypatch.setattr(verify, "_sql_parser", "sqlparse")
        ok, detail = verify.sql_syntax_check("SELECT 1")
        assert ok is False
        assert detail


# ---------------------------------------------------------------------------
# Bash: validated when bash is on PATH; genuine syntax errors still block.
# ---------------------------------------------------------------------------

class TestBashValidation:
    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
    def test_clean_bash_block_is_verified(self):
        r = verify.verify_answer("```bash\necho ok\n```")
        assert r["passed"] is True
        assert r["verified"] is True
        assert r["unverified_languages"] == []

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
    def test_bash_block_with_genuine_syntax_error_blocks(self):
        broken = "```bash\nif [ 1 -eq 1 ]; then\necho hi\n```"
        r = verify.verify_answer(broken)
        assert r["passed"] is False
        assert r["verified"] is False

    def test_bash_syntax_check_reports_unavailable_when_bash_missing(self, monkeypatch):
        # Simulate "bash not on PATH" without depending on the real
        # environment's PATH -- FileNotFoundError from subprocess.run.
        import subprocess as _subprocess

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("bash not found")

        monkeypatch.setattr(_subprocess, "run", fake_run)
        ok, detail = verify.bash_syntax_check("echo ok")
        assert ok is None
        assert detail == "bash not available"

    def test_bash_unavailable_degrades_to_unverified_not_exception(self, monkeypatch):
        monkeypatch.setattr(verify, "bash_syntax_check", lambda code, timeout=8: (None, "bash not available"))
        r = verify.verify_answer("```bash\necho ok\n```")
        assert r["passed"] is True
        assert r["verified"] is False
        assert r["unverified_languages"] == ["bash"]


# ---------------------------------------------------------------------------
# Python: untagged and py-tagged blocks keep the exact existing path.
# ---------------------------------------------------------------------------

class TestPythonPathUnchanged:
    def test_untagged_valid_python_fence_passes_and_verifies(self):
        r = verify.verify_answer("```\nprint('hi')\n```")
        assert r["passed"] is True
        assert r["verified"] is True
        assert r["unverified_languages"] == []

    def test_py_tagged_valid_python_fence_passes_and_verifies(self):
        r = verify.verify_answer("```py\nx = 1\n```")
        assert r["passed"] is True
        assert r["verified"] is True
        assert r["unverified_languages"] == []

    def test_genuine_python_syntax_error_still_blocks(self):
        r = verify.verify_answer("```py\nx =\n```")
        assert r["passed"] is False
        assert r["verified"] is False
        assert r["syntax_ok"] is False


# ---------------------------------------------------------------------------
# Prose-only and mixed-language answers.
# ---------------------------------------------------------------------------

class TestProseAndMixed:
    def test_prose_only_has_no_code_and_is_verified(self):
        r = verify.verify_answer("just some prose, no fences at all")
        assert r["has_code"] is False
        assert r["passed"] is True
        assert r["verified"] is True
        assert r["unverified_languages"] == []

    def test_mixed_valid_python_and_js_block_is_non_blocking_but_unverified(self):
        text = "```python\nprint('hi')\n```\n\nand also:\n\n```js\nvar x = 1;\n```"
        r = verify.verify_answer(text)
        assert r["passed"] is True
        assert r["verified"] is False
        assert r["unverified_languages"] == ["js"]


# ---------------------------------------------------------------------------
# verified_gate.py's repair-loop contract stays byte-for-byte: only a
# genuine `passed=False` should ever set needs_repair.
# ---------------------------------------------------------------------------

class TestNonBlockingHonestyProperty:
    def test_unverified_language_never_flips_passed_to_false(self):
        # sql excluded from this generic loop: since Phase 41-02 installs
        # sqlglot, "sql" is no longer a no-validator tag in this environment
        # (`_sql_parser == "sqlglot"` -- WR-04) and "some code" happens to be
        # lenient-parseable SQL, so it would genuinely verify here. sql's
        # non-blocking-honesty contract is covered separately below with the
        # parser explicitly disabled, matching this file's existing
        # `_sql_parser`-gated convention for SQL-specific assertions.
        for tag in ("js", "ts", "html"):
            r = verify.verify_answer(f"```{tag}\nsome code\n```")
            assert r["passed"] is True, f"{tag} unexpectedly blocked"
            assert r["verified"] is False

    def test_sql_unverified_when_no_parser_never_flips_passed_to_false(self, monkeypatch):
        monkeypatch.setattr(verify, "_sql_parser", None)
        r = verify.verify_answer("```sql\nsome code\n```")
        assert r["passed"] is True, "sql unexpectedly blocked"
        assert r["verified"] is False


# ---------------------------------------------------------------------------
# SEC-09 (Phase 20): run_snippet must not leak the app's secrets to the
# executed subprocess (env allowlist scrub) and must cap its resource usage
# on POSIX (rlimit via preexec_fn); the execution tier itself must stay
# opt-in/off-by-default (regression-lock). Targets `verify.run_snippet`
# directly -- mirrors this file's existing direct-call style.
# ---------------------------------------------------------------------------

class TestSEC09ExecutionHardening:
    def test_canary_env_is_scrubbed_from_executed_snippet(self, monkeypatch):
        # A distinctive secret-shaped env var set on the PARENT process must
        # NOT be visible inside the executed child -- proves the child's env
        # is scrubbed to an allowlist, not inherited wholesale from
        # os.environ. Cross-platform (no skip): the snippet itself exits
        # non-zero if the canary is visible, so run_snippet's return value
        # (not stdout, which run_snippet discards on success) proves scrub.
        monkeypatch.setenv("PRISM_CANARY_SECRET", "super-secret-parent-value")
        code = (
            "import os, sys\n"
            "val = os.environ.get('PRISM_CANARY_SECRET')\n"
            "sys.exit(1 if val is not None else 0)\n"
        )
        ok, detail = verify.run_snippet(code)
        assert ok is True, f"canary secret leaked into executed snippet env: {detail}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource module only")
    def test_rlimit_kills_over_cap_allocation(self):
        # Attempt to allocate well beyond the RLIMIT_AS cap (512MB per
        # 20-PATTERNS.md) -- must be killed/fail, proving resource.setrlimit
        # is genuinely enforced via the preexec_fn hook, not merely
        # constructed and discarded.
        code = "bytearray(1024 * 1024 * 1024 * 2)\n"  # 2GB, over the 512MB cap
        ok, detail = verify.run_snippet(code)
        assert ok is False, "over-cap allocation should have been killed/failed by rlimit"

    def test_verify_execute_and_agent_verify_execute_default_off(self):
        # Regression-lock: the unsandboxed execution tier must never be on
        # unless an admin explicitly opts in. Static-source assertions --
        # deliberately does NOT flip either flag on globally via a fixture.
        import inspect

        import routes.council_routes as council_routes
        import src.agent_loop as agent_loop

        run_request_src = inspect.getsource(council_routes.RunRequest)
        assert "verify_execute: bool = False" in run_request_src

        agent_loop_src = inspect.getsource(agent_loop)
        assert 'get_setting("agent_verify_execute", False)' in agent_loop_src


# ---------------------------------------------------------------------------
# WR-02 (Phase 20 gap-closure, hardens SEC-09): RLIMIT_CPU must derive from
# the caller's `run_snippet` timeout (+1s grace), not the old hardcoded 8s --
# a raised wall-clock budget must raise the CPU ceiling too, or a legitimate
# CPU-bound snippet is killed by SIGXCPU and misreported as a verify failure.
# ---------------------------------------------------------------------------

class TestWR02CpuRlimitTracksTimeout:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX resource module only")
    def test_rlimit_preexec_fn_derives_cpu_cap_from_argument(self, monkeypatch):
        import resource

        calls = []
        monkeypatch.setattr(
            resource, "setrlimit", lambda res, val: calls.append((res, val))
        )

        apply_fn = verify._rlimit_preexec_fn(13)
        apply_fn()

        cpu_calls = [val for res, val in calls if res == resource.RLIMIT_CPU]
        assert cpu_calls == [(13, 13)], "RLIMIT_CPU must be set to the passed-in cap exactly"
        # Other caps stay fixed/unrelated to the cpu_seconds argument.
        as_calls = [val for res, val in calls if res == resource.RLIMIT_AS]
        assert as_calls == [(512 * 1024 * 1024,) * 2]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only preexec_fn wiring")
    def test_run_snippet_threads_default_timeout_into_rlimit_with_grace(self, monkeypatch):
        calls = []
        real_fn = verify._rlimit_preexec_fn

        def _spy(cpu_seconds):
            calls.append(cpu_seconds)
            return real_fn(cpu_seconds)

        monkeypatch.setattr(verify, "_rlimit_preexec_fn", _spy)
        ok, detail = verify.run_snippet("import sys; sys.exit(0)\n")  # default timeout=8

        assert ok is True, detail
        assert calls == [9], "default 8s timeout must map to a 9s CPU grace (byte-for-byte preserved default)"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only preexec_fn wiring")
    def test_run_snippet_threads_raised_timeout_into_rlimit_with_grace(self, monkeypatch):
        calls = []
        real_fn = verify._rlimit_preexec_fn

        def _spy(cpu_seconds):
            calls.append(cpu_seconds)
            return real_fn(cpu_seconds)

        monkeypatch.setattr(verify, "_rlimit_preexec_fn", _spy)
        ok, detail = verify.run_snippet("import sys; sys.exit(0)\n", timeout=20)

        assert ok is True, detail
        assert calls == [21], "raising timeout must raise the CPU cap by the same grace margin"
