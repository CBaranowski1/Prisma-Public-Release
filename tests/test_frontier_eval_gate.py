"""Focused tests for the versioned frontier deployment gate."""

import importlib.util
import json
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "frontier_eval_gate",
    Path("scripts/frontier_eval_gate.py"),
)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_offline_contract_suite_passes_all_fixed_workflows():
    suite = gate.load_suite()
    results = gate.run_offline(suite)
    summary = gate.summarize(suite, "offline", results)

    assert len(results) == 6
    assert summary["metrics"]["pass_rate"] == 1.0
    assert summary["metrics"]["tool_correctness"] == 1.0
    assert summary["metrics"]["grounding_rate"] == 1.0
    assert summary["metrics"]["retrieval_usefulness"] == 1.0
    assert gate.evaluate_thresholds(summary, suite["thresholds"]["offline"]) == []


def test_suite_is_schema_versioned_and_covers_required_dimensions():
    suite = gate.load_suite()

    assert suite["schema_version"] == 1
    assert {case["dimension"] for case in suite["cases"]} == {
        "skill_inventory",
        "brain_recall",
        "reasoning_route",
        "tool_choice",
        "provenance",
        "recovery",
    }
    assert suite["thresholds"]["live"]["min_tokens_per_second"] > 0


def test_baseline_comparison_separates_capability_from_latency_regression():
    suite = gate.load_suite()
    baseline = {
        "suite_id": suite["suite_id"],
        "metrics": {
            "pass_rate": 1.0,
            "tool_correctness": 1.0,
            "grounding_rate": 1.0,
            "retrieval_usefulness": 1.0,
            "median_wall_time_ms": 1000.0,
            "tokens_per_second": 10.0,
        },
    }
    current = {
        "suite_id": suite["suite_id"],
        "metrics": {
            "pass_rate": 0.8,
            "tool_correctness": 1.0,
            "grounding_rate": 1.0,
            "retrieval_usefulness": 1.0,
            "median_wall_time_ms": 1300.0,
            "tokens_per_second": 7.0,
        },
    }

    regressions = gate.compare_to_baseline(current, baseline, suite["regression_limits"])

    assert any("pass_rate" in item for item in regressions["capability"])
    assert any("median_wall_time_ms" in item for item in regressions["latency"])
    assert any("tokens_per_second" in item for item in regressions["latency"])


def test_live_response_records_backend_or_usage_throughput():
    parsed = gate._extract_live_response(
        {
            "choices": [{"message": {"content": "done"}}],
            "usage": {"completion_tokens": 20},
        },
        wall_seconds=4.0,
    )
    backend = gate._extract_live_response(
        {
            "choices": [{"message": {"content": "done"}}],
            "usage": {"completion_tokens": 20},
            "timings": {"predicted_per_second": 12.5},
        },
        wall_seconds=4.0,
    )

    assert parsed["tokens_per_second"] == 5.0
    assert backend["tokens_per_second"] == 12.5


def test_cli_returns_nonzero_for_capability_threshold_failure(tmp_path, monkeypatch):
    suite = gate.load_suite()
    suite["thresholds"]["offline"]["min_pass_rate"] = 1.1
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")
    monkeypatch.setattr(gate, "run_offline", lambda loaded: [
        {
            "task_id": "only",
            "dimension": "tool_choice",
            "passed": True,
            "detail": "",
            "wall_time_ms": 1.0,
            "tool_correct": True,
            "grounding_status": "grounded",
            "retrieval_useful": True,
            "output_tokens": None,
            "tokens_per_second": None,
            "turns_to_completion": 1,
            "endpoint_recovered": False,
        }
    ])

    assert gate.main(["--mode", "offline", "--suite", str(suite_path)]) == 1
