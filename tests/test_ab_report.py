"""MEAS-02 edge-case coverage for scripts/ab_report.py's pure functions —
`_mcnemar_p` (exact two-sided binomial McNemar p-value over discordant pairs)
and `_load` (JSONL -> {task_id: bool(passed)} loader).

scripts/ab_report.py itself is UNCHANGED by this plan (reused verbatim as the
MEAS-02 report layer) — this file only characterizes its existing math. No
live Ollama, no network, stdlib + pytest only.

Import strategy: try the namespace-package import first (`scripts/` has no
`__init__.py` but is an implicit PEP 420 namespace package once the repo root
is on sys.path, which tests/conftest.py already guarantees); fall back to
importlib.util.spec_from_file_location on the absolute path, the same
dynamic-load convention `providers/precision_vault.py` and the Phase 11 test
(`tests/test_verify_hardening_integration.py`) already use.
"""
from __future__ import annotations

import importlib.util
import json
import math
import pathlib

try:
    from scripts import ab_report
except ImportError:
    _spec = importlib.util.spec_from_file_location(
        "ab_report", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "ab_report.py")
    ab_report = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ab_report)


# ---------------------------------------------------------------------------
# _mcnemar_p edge cases
# ---------------------------------------------------------------------------

def test_mcnemar_p_n_zero_guard():
    """n == b + c == 0 (no discordant pairs at all) -> p == 1.0, the explicit
    guard clause in _mcnemar_p, not a division/degenerate-binomial fallthrough."""
    assert ab_report._mcnemar_p(0, 0) == 1.0


def test_mcnemar_p_fully_concordant_is_one():
    """Fully-concordant case (b=c=0) is the same n==0 guard from the report's
    own semantics (no problems flipped either direction) -> p == 1.0."""
    assert ab_report._mcnemar_p(0, 0) == 1.0


def test_mcnemar_p_symmetric_in_its_arguments():
    """_mcnemar_p(b, c) == _mcnemar_p(c, b) for any b > 0 — the exact binomial
    tail only depends on n=b+c and k=min(b,c), both symmetric quantities."""
    for b, c in [(1, 3), (5, 2), (7, 7), (10, 1)]:
        assert ab_report._mcnemar_p(b, c) == ab_report._mcnemar_p(c, b)


def test_mcnemar_p_valid_two_sided_probability_when_equal():
    """_mcnemar_p(b, b) for any b > 0 (perfectly balanced discordant pairs) is
    a valid two-sided p-value in (0, 1] — maximally NOT reliable evidence of
    a real difference, since the flips split evenly both ways."""
    for b in (1, 2, 5, 10):
        p = ab_report._mcnemar_p(b, b)
        assert 0.0 < p <= 1.0


def test_mcnemar_p_large_asymmetric_split_is_reliable():
    """A large, one-sided split (10 helped, 0 hurt) -> p < 0.05, the report's
    own RELIABLE threshold."""
    p = ab_report._mcnemar_p(10, 0)
    assert p < 0.05


def test_mcnemar_p_min_bc_zero_matches_closed_form():
    """When min(b, c) == 0 (asymmetric, e.g. b=8, c=0), the exact tail
    collapses to the k=0 term only: 2 * (1 / 2**n), capped at 1.0."""
    b, c = 8, 0
    n = b + c
    expected = min(1.0, 2 * (1 / (2 ** n)))
    assert ab_report._mcnemar_p(b, c) == expected
    assert math.isclose(ab_report._mcnemar_p(b, c), expected, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# _load edge cases
# ---------------------------------------------------------------------------

def test_load_parses_jsonl_and_coerces_bool(tmp_path):
    """_load reads a {task_id, passed} JSONL fixture into {task_id: bool},
    coercing a missing/false 'passed' key to False and a truthy one to True."""
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text(
        json.dumps({"task_id": "t1", "passed": True}) + "\n"
        + json.dumps({"task_id": "t2", "passed": False}) + "\n",
        encoding="utf-8",
    )
    result = ab_report._load(str(fixture))
    assert result == {"t1": True, "t2": False}
    assert isinstance(result["t1"], bool)
    assert isinstance(result["t2"], bool)


def test_load_missing_passed_key_coerces_false(tmp_path):
    """A row with no 'passed' key at all (e.g. an errored/incomplete run) must
    coerce to False, not raise a KeyError or leave a None."""
    fixture = tmp_path / "fixture_missing.jsonl"
    fixture.write_text(json.dumps({"task_id": "t3"}) + "\n", encoding="utf-8")
    result = ab_report._load(str(fixture))
    assert result == {"t3": False}
    assert result["t3"] is False
