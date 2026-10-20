# tests/test_verify_ruby_exec.py
"""Hermetic proof for the Ruby EXECUTION tier (D-09, Phase 41 Plan 01).

Targets `src.council.verify.verify_answer` / `ruby_exec_check` BY NAME and
`src.verified_gate.evaluate` -- mirrors the direct-call style of
tests/test_council_verify.py (SEC-08's sibling suite) and the
BROKEN_PY/VALID_PY scripting convention of tests/test_council_escalation.py,
adapted to Ruby.

D-09: research (41-RESEARCH.md Pattern 2) recommended a syntax-only `ruby -c`
tier. The user chose the HEAVIER execution tier instead -- runtime failures
(not just malformed syntax) must also flip `needs_repair`, while a
syntactically-valid-but-logically-wrong Ruby method must stay non-blocking
(verify.py's D-02 "no hidden tests in a general council prompt" contract).

The live-ruby cases (1-5) are skipped when `ruby` isn't on PATH so this suite
stays green in a ruby-less CI; case (6) patches `subprocess.run` directly and
runs unconditionally.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

import src.council.verify as verify
from src.verified_gate import evaluate

_HAS_RUBY = shutil.which("ruby") is not None
_needs_ruby = pytest.mark.skipif(not _HAS_RUBY, reason="ruby not on PATH")

# An unterminated method header -- fails to parse, mirrors 41-RESEARCH.md's
# empirically-verified R-2 repro (`def foo(\n  bar\n`).
BROKEN_RUBY = "Here:\n```ruby\ndef foo(\n  bar\n```"

# Parses cleanly but raises at top level when loaded -- the execution tier
# (not `ruby -c`) is what catches this; a syntax-only tier would pass it.
CRASHING_RUBY = "Here:\n```ruby\nraise \"boom\"\n```"

# A syntactically-valid method whose logic is wrong (subtracts instead of
# adds) but which loads/defines cleanly with no top-level call -- must stay
# passed=True (no hidden tests, D-02).
WRONG_LOGIC_RUBY = "Here:\n```ruby\ndef add(a, b)\n  a - b\nend\n```"


class TestRubyExecutionTierBlocksBrokenAndCrashingCode:
    @_needs_ruby
    def test_broken_ruby_fence_fails_and_blocks(self):
        r = verify.verify_answer(BROKEN_RUBY, execute=True)
        assert r["has_code"] is True
        assert r["syntax_ok"] is False, "unparseable Ruby must not report syntax_ok=True"
        assert r["passed"] is False, "a broken Ruby fence must flip passed to False"

        verdict, needs_repair, why = evaluate(BROKEN_RUBY, execute=True)
        assert needs_repair is True, (
            "verified_gate.evaluate must derive needs_repair=True from a "
            f"failed Ruby verdict, got verdict={verdict}"
        )
        assert why is not None

    @_needs_ruby
    def test_top_level_crashing_ruby_fence_fails_execution_tier(self):
        # Syntactically valid (parses clean) but raises when loaded -- this
        # is the D-09 execution-tier behavior that goes beyond `ruby -c`.
        r = verify.verify_answer(CRASHING_RUBY, execute=True)
        assert r["passed"] is False, (
            "a Ruby fence that raises at top level must fail the execution "
            f"tier even though it parses cleanly, got {r}"
        )

    @_needs_ruby
    def test_valid_but_logically_wrong_ruby_stays_non_blocking(self):
        # No hidden tests in a general council prompt (verify.py D-02) --
        # a def that loads without crashing passes even if its logic is
        # wrong, because nothing here ever calls it or checks its output.
        r = verify.verify_answer(WRONG_LOGIC_RUBY, execute=True)
        assert r["passed"] is True, f"logically-wrong-but-loading Ruby must not block, got {r}"
        assert r["verified"] is True, (
            "a genuinely-executed clean Ruby block must count as verified "
            f"(ruby must NOT remain in unverified_languages), got {r}"
        )
        assert "ruby" not in r["unverified_languages"]


class TestRubyExecutionTierSandboxPosture:
    @_needs_ruby
    def test_env_is_scrubbed_from_executed_ruby_child(self, monkeypatch):
        # A sentinel secret-shaped env var set on the PARENT process must not
        # be visible inside the executed Ruby child. The snippet itself
        # exits non-zero IFF the probe key is visible, so a passing verdict
        # proves the scrub (mirrors TestSEC09ExecutionHardening's canary
        # pattern in tests/test_council_verify.py, adapted to Ruby ENV).
        monkeypatch.setenv("PRISM_SECRET_PROBE", "super-secret-parent-value")
        probe_ruby = (
            "Here:\n```ruby\n"
            "if ENV['PRISM_SECRET_PROBE']\n"
            "  exit 1\n"
            "else\n"
            "  exit 0\n"
            "end\n"
            "```"
        )
        r = verify.verify_answer(probe_ruby, execute=True)
        assert r["passed"] is True, (
            "PRISM_SECRET_PROBE leaked into the executed Ruby child's env -- "
            f"scrub failed, got {r}"
        )

    @_needs_ruby
    def test_hanging_ruby_fence_times_out(self):
        sleepy_ruby = "Here:\n```ruby\nsleep 5\n```"
        r = verify.verify_answer(sleepy_ruby, execute=True, timeout=1)
        assert r["passed"] is False, "a hanging Ruby snippet must never pass"
        assert "timeout" in r["detail"].lower(), (
            f"expected a timeout-shaped detail, got detail={r['detail']!r}"
        )


class TestRubyExecutionTierGracefulDegrade:
    def test_ruby_absent_degrades_to_honest_unverified_never_hard_fails(self, monkeypatch):
        # Simulate "ruby not on PATH" without depending on the real
        # environment -- FileNotFoundError from subprocess.run, exactly the
        # bash_syntax_check precedent's graceful-degrade test shape.
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("ruby not found")

        monkeypatch.setattr(subprocess, "run", fake_run)

        r = verify.verify_answer(WRONG_LOGIC_RUBY, execute=True)
        assert r["passed"] is True, (
            "an unavailable Ruby interpreter must never hard-fail the gate, "
            f"got {r}"
        )
        assert "ruby" in r["unverified_languages"]
        assert r["verified"] is False
