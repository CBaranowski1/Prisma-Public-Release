"""Phase 33 baseline integration-checker (SC-5, DEBT-03).

Cheap, hermetic recorded-count + order-stability check per research Open
Question 2: asserts the counts already recorded in the repo-root
BASELINE.md, and proves order-stability only for the small, previously
-polluted file pairs (via narrowly-scoped `subprocess` re-runs) -- this
test deliberately does NOT recursively re-run the whole ~275s suite
inside itself (that would roughly double CI time every run).

Hermetic: reads BASELINE.md from disk, subprocess-runs a handful of test
files at a time (a few seconds each), never the full suite.
"""
import re
import subprocess
import sys
from pathlib import Path

BASELINE_PATH = Path(__file__).resolve().parent.parent / "BASELINE.md"

# The two previously-polluted target+polluter groups this phase fixed
# (33-02 Pattern 1, 33-03/33-08 Pattern 2). Each file must pass both
# standalone and together with its former polluter(s) -- proving the
# fix is genuinely order-stable, not order-dependent in a new direction.
PATTERN_1_PAIR = [
    "tests/test_ide_token.py",
    "tests/test_image_gen_server_data_dir.py",
    "tests/test_phase26_integration.py",
]

PATTERN_2_PAIR = [
    "tests/test_auth_config_lock_concurrency.py",
    "tests/test_tool_run_query.py",
    "tests/test_verify_qb_spine_integration.py",
]

REPO_ROOT = Path(__file__).resolve().parent.parent


def _baseline_text() -> str:
    assert BASELINE_PATH.is_file(), f"BASELINE.md not found at {BASELINE_PATH}"
    return BASELINE_PATH.read_text(encoding="utf-8")


def _run_pytest(files):
    """Run pytest against a small, explicit file list -- never the full suite."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Recorded-count assertions (read BASELINE.md, never re-run the full suite)
# ---------------------------------------------------------------------------

def test_baseline_md_states_zero_unexplained_failures_invariant():
    text = _baseline_text()
    assert "zero unexplained failures" in text.lower(), (
        "BASELINE.md must explicitly state the zero-unexplained-failures invariant"
    )


def test_baseline_md_records_pass_skip_counts_and_zero_failed():
    text = _baseline_text()
    summary = re.search(r"(\d+) passed, (\d+) skipped, \d+ warnings in [\d.]+s", text)
    assert summary, "BASELINE.md must record a verbatim pytest summary line"
    passed, skipped = int(summary.group(1)), int(summary.group(2))
    assert passed > 0, "recorded baseline must have passing tests"
    assert skipped >= 0

    failed = re.search(r"\*\*Failed:\*\*\s*(\d+)", text)
    assert failed, "BASELINE.md must record an explicit Failed count"
    assert int(failed.group(1)) == 0, "recorded baseline must show zero failures"


def test_baseline_md_skip_ledger_total_matches_recorded_skip_count():
    text = _baseline_text()
    summary = re.search(r"\d+ passed, (\d+) skipped,", text)
    assert summary
    recorded_skipped = int(summary.group(1))

    ledger_total = re.search(r"\*\*Total:\s*(\d+)\.\*\*", text)
    assert ledger_total, "BASELINE.md Skip Ledger must record an explicit total"
    assert int(ledger_total.group(1)) == recorded_skipped, (
        "Skip Ledger total must match the recorded summary-line skip count "
        "-- every skip must be individually accounted for"
    )


def test_baseline_md_known_reds_section_present_and_honest():
    text = _baseline_text()
    assert "## Known-Reds" in text, "BASELINE.md must have a Known-Reds section"
    section = text.split("## Known-Reds", 1)[1].split("\n## ", 1)[0]
    # As of this baseline, 33-08 resolved all previously-named known-reds.
    # This assertion enforces the honest documentation: if a future baseline
    # re-introduces a known-red, this line (or the section content) must be
    # updated to name it explicitly rather than silently going stale.
    assert "None" in section, (
        "Known-Reds section must explicitly say 'None' when no known-reds "
        "remain, or name them explicitly otherwise"
    )


# ---------------------------------------------------------------------------
# Order-stability assertions (small subprocess re-runs, never the full suite)
# ---------------------------------------------------------------------------

def test_pattern1_pair_passes_together():
    result = _run_pytest(PATTERN_1_PAIR)
    assert result.returncode == 0, (
        f"Pattern-1 pair failed when run together:\n{result.stdout}\n{result.stderr}"
    )


def test_pattern1_files_pass_standalone():
    for f in PATTERN_1_PAIR:
        result = _run_pytest([f])
        assert result.returncode == 0, (
            f"{f} failed standalone:\n{result.stdout}\n{result.stderr}"
        )


def test_pattern2_pair_passes_together():
    result = _run_pytest(PATTERN_2_PAIR)
    assert result.returncode == 0, (
        f"Pattern-2 pair failed when run together:\n{result.stdout}\n{result.stderr}"
    )


def test_pattern2_files_pass_standalone():
    for f in PATTERN_2_PAIR:
        result = _run_pytest([f])
        assert result.returncode == 0, (
            f"{f} failed standalone:\n{result.stdout}\n{result.stderr}"
        )
