"""Versioned frontier-architecture deployment gate.

This complements (and does not replace) ``scripts/ab_harness.py`` and the
polyglot A/B harness.  Those measure model/code-task lift; this fast gate
checks that Prisma's architecture contracts remain wired and emits the same
schema for deterministic CI and live OpenAI-compatible GLM runs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = REPO_ROOT / "scripts" / "evals" / "frontier_contract_v1.json"
RESULT_SCHEMA_VERSION = 1

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_suite(path: str | Path = DEFAULT_SUITE) -> dict:
    """Load and validate the small, versioned contract dataset."""
    with Path(path).open(encoding="utf-8") as handle:
        suite = json.load(handle)
    if suite.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported suite schema_version={suite.get('schema_version')!r}; "
            f"expected {RESULT_SCHEMA_VERSION}"
        )
    if not suite.get("suite_id") or not isinstance(suite.get("cases"), list):
        raise ValueError("suite_id and cases are required")
    ids = [case.get("id") for case in suite["cases"]]
    if not ids or any(not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("case ids must be present and unique")
    required_dimensions = {
        "skill_inventory", "brain_recall", "reasoning_route",
        "tool_choice", "provenance", "recovery",
    }
    missing = required_dimensions - {case.get("dimension") for case in suite["cases"]}
    if missing:
        raise ValueError(f"suite missing required dimensions: {sorted(missing)}")
    for mode in ("offline", "live"):
        if mode not in suite.get("thresholds", {}):
            raise ValueError(f"suite thresholds missing {mode!r}")
    return suite


def _result(
    case: dict,
    passed: bool,
    started: float,
    *,
    detail: str = "",
    tool_correct: bool | None = None,
    grounding_status: str | None = None,
    retrieval_useful: bool | None = None,
    output_tokens: int | None = None,
    tokens_per_second: float | None = None,
    turns: int = 1,
    endpoint_recovered: bool = False,
) -> dict:
    return {
        "task_id": case["id"],
        "dimension": case["dimension"],
        "passed": bool(passed),
        "detail": detail,
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
        "tool_correct": tool_correct,
        "grounding_status": grounding_status,
        "retrieval_useful": retrieval_useful,
        "output_tokens": output_tokens,
        "tokens_per_second": tokens_per_second,
        "turns_to_completion": turns,
        "endpoint_recovered": endpoint_recovered,
    }


def _offline_skill(case: dict) -> dict:
    from services.memory.skills import SkillsManager

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="prisma-frontier-skills-") as tmp:
        manager = SkillsManager(
            str(Path(tmp) / "data"),
            bundled_skills_dir=str(REPO_ROOT / ".claude" / "skills"),
        )
        marker = "CALL_BRAIN_SEARCH_WITH_OWNER_SCOPE"
        manager.add_skill(
            name="second-brain-operator",
            description="Search all configured brain vaults and report provider health.",
            when_to_use="Use when asked to find notes or inspect the second brain.",
            procedure=[marker],
            status="published",
            source="user",
            owner="frontier-eval",
        )
        index = manager.index_for(owner="frontier-eval")
        names = {item["name"] for item in index}
        compact = all(
            set(item) <= {"name", "description", "category", "status"}
            for item in index
        )
        selected = manager.get_relevant_skills(
            case["prompt"],
            skills=manager.load(owner="frontier-eval"),
            threshold=0.0,
            max_items=1,
        )
        selected_name = selected[0]["name"] if selected else ""
        body = manager.read_skill_md(selected_name, owner="frontier-eval") or ""
        passed = (
            len(index) >= 28
            and "code-review" in names
            and "second-brain-operator" in names
            and compact
            and selected_name == "second-brain-operator"
            and marker in body
        )
        return _result(
            case,
            passed,
            started,
            detail=f"inventory={len(index)} selected={selected_name} compact={compact}",
            retrieval_useful=selected_name == "second-brain-operator" and marker in body,
        )


def _offline_brain(case: dict) -> dict:
    from src.brain_index import _hybrid_score, _lexical_relevance

    started = time.perf_counter()
    rows = [
        {
            "id": "architecture-advisor",
            "title": "Architecture Advisor",
            "category": "vera-agents",
            "excerpt": "A general VERA specialist profile.",
            "semantic_score": 1.0,
        },
        {
            "id": "AI-SPEC",
            "title": "Prisma frontier architecture contract",
            "category": "imported",
            "excerpt": "Implementation and evaluation guidance.",
            "semantic_score": 0.0,
        },
    ]
    # Mirror BrainIndex.search's ranking tuple: lexical confidence breaks a
    # hybrid-score tie caused by a stale unrelated vector at cosine 1.0.
    ranked = sorted(
        rows,
        key=lambda row: (
            _hybrid_score(
                row["semantic_score"], _lexical_relevance(case["prompt"], row)
            ),
            _lexical_relevance(case["prompt"], row),
            row["semantic_score"],
        ),
        reverse=True,
    )
    selected = ranked[0]["id"]
    useful = selected == "AI-SPEC"
    return _result(
        case,
        useful,
        started,
        detail=f"selected={selected}",
        retrieval_useful=useful,
    )


def _offline_reasoning(case: dict) -> dict:
    from routes.chat_routes import _resolve_reasoning_effort

    started = time.perf_counter()
    checks = [
        (_resolve_reasoning_effort("auto", "hello there")[0], "low"),
        (_resolve_reasoning_effort("auto", case["prompt"])[0], "high"),
        (_resolve_reasoning_effort("auto", "check my tools", agent_mode=True)[0], "high"),
        (_resolve_reasoning_effort("max", "hello there")[0], "max"),
    ]
    passed = all(actual == expected for actual, expected in checks)
    return _result(case, passed, started, detail=f"routes={checks}")


def _offline_tool(case: dict) -> dict:
    from src.action_intents import classify_tool_intent

    started = time.perf_counter()
    intent = classify_tool_intent(case["prompt"])
    correct = intent.needs_tools and intent.category == "calendar"
    return _result(
        case,
        correct,
        started,
        detail=f"needs_tools={intent.needs_tools} category={intent.category}",
        tool_correct=correct,
    )


def _offline_provenance(case: dict) -> dict:
    from src.provenance import assess_response, build_evidence_ledger

    started = time.perf_counter()
    ledger = build_evidence_ledger(web_sources=[{
        "title": "Maintenance bulletin",
        "url": "https://example.invalid/maintenance",
        "snippet": "Scheduled maintenance begins Sunday at 03:00 UTC.",
    }])
    source_id = ledger[0]["source_id"]
    valid = assess_response(f"Sunday at 03:00 UTC [source:{source_id}]", ledger)
    forged = assess_response("Sunday at 03:00 UTC [source:made-up]", ledger)
    passed = (
        valid["grounding_status"] == "grounded"
        and valid["invalid_citation_count"] == 0
        and forged["grounding_status"] == "none"
        and forged["invalid_source_ids"] == ["made-up"]
    )
    return _result(
        case,
        passed,
        started,
        detail=(
            f"valid={valid['grounding_status']} "
            f"forged={forged['grounding_status']}"
        ),
        grounding_status=valid["grounding_status"] if passed else "none",
    )


def _offline_recovery(case: dict) -> dict:
    import src.llm_core as llm_core
    from src.context_compactor import trim_for_context

    started = time.perf_counter()
    nonce = "context-omega-91"
    messages = [{"role": "system", "content": "Keep the current user turn."}]
    messages.extend(
        {"role": "user" if idx % 2 == 0 else "assistant", "content": "old " * 1600}
        for idx in range(14)
    )
    messages.append({"role": "user", "content": f"Recovery token: {nonce}"})
    trimmed = trim_for_context(messages, context_length=1800, reserve_tokens=256)
    current_survived = nonce in json.dumps(trimmed)

    candidates = [
        ("http://primary.invalid/v1/chat/completions", "glm", {}),
        ("http://primary.invalid/v1/chat/completions", "glm", {"X-Duplicate": "1"}),
        ("http://fallback.invalid/v1/chat/completions", "glm", {}),
    ]
    attempts: list[str] = []
    original_call = llm_core.llm_call

    def fake_call(url, _model, _messages, **_kwargs):
        attempts.append(url)
        if "primary" in url:
            raise RuntimeError("simulated primary failure")
        return "recovered"

    try:
        llm_core.llm_call = fake_call
        answer = llm_core.llm_call_with_fallback(candidates, [{"role": "user", "content": "go"}])
    finally:
        llm_core.llm_call = original_call

    recovered = answer == "recovered" and attempts == [candidates[0][0], candidates[2][0]]
    passed = current_survived and recovered
    return _result(
        case,
        passed,
        started,
        detail=f"current_survived={current_survived} attempts={len(attempts)}",
        endpoint_recovered=recovered,
    )


OFFLINE_CONTRACTS: dict[str, Callable[[dict], dict]] = {
    "skill_progressive_disclosure": _offline_skill,
    "brain_hybrid_recall": _offline_brain,
    "reasoning_route": _offline_reasoning,
    "tool_choice": _offline_tool,
    "provenance": _offline_provenance,
    "context_failure_recovery": _offline_recovery,
}


def run_offline(suite: dict) -> list[dict]:
    """Exercise deterministic production contracts without a model/network."""
    results = []
    for case in suite["cases"]:
        contract = case.get("offline_contract")
        evaluator = OFFLINE_CONTRACTS.get(contract)
        if evaluator is None:
            raise ValueError(f"unknown offline contract {contract!r} for {case['id']}")
        results.append(evaluator(case))
    return results


def _completion_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("endpoint must be an http(s) URL without embedded credentials")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        if path.endswith("/v1"):
            path += "/chat/completions"
        else:
            path += "/v1/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _post_completion(
    endpoint: str,
    payload: dict,
    *,
    api_key: str = "",
    timeout: float = 180.0,
) -> tuple[dict, float]:
    url = _completion_url(endpoint)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        data = json.loads(response.read())
    return data, time.perf_counter() - started


def _extract_live_response(data: dict, wall_seconds: float) -> dict:
    message = data.get("choices", [{}])[0].get("message", {})
    content = message.get("content") or message.get("reasoning_content") or ""
    tool_calls = message.get("tool_calls") or []
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    try:
        output_tokens = int(output_tokens) if output_tokens is not None else None
    except (TypeError, ValueError):
        output_tokens = None
    backend_tps = None
    timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
    for candidate in (
        timings.get("predicted_per_second"),
        usage.get("tokens_per_second"),
        usage.get("output_tokens_per_second"),
    ):
        if isinstance(candidate, (int, float)) and candidate >= 0:
            backend_tps = float(candidate)
            break
    if backend_tps is None and output_tokens is not None and wall_seconds > 0:
        backend_tps = output_tokens / wall_seconds
    return {
        "content": str(content),
        "tool_calls": tool_calls,
        "output_tokens": output_tokens,
        "tokens_per_second": round(backend_tps, 3) if backend_tps is not None else None,
    }


def _first_tool(tool_calls: list) -> tuple[str, str]:
    if not tool_calls or not isinstance(tool_calls[0], dict):
        return "", ""
    function = tool_calls[0].get("function") or {}
    return str(function.get("name") or ""), str(function.get("arguments") or "")


def _live_payload(case: dict, model: str) -> tuple[dict, list[dict]]:
    from src.provenance import build_evidence_ledger, prompt_messages

    spec = case["live"]
    messages = [{"role": "system", "content": spec.get("system", "Follow the user request.")}]
    ledger = build_evidence_ledger(web_sources=spec.get("evidence") or [])
    messages.extend(prompt_messages(ledger))
    repeat = str(spec.get("context_repeat") or "") * int(spec.get("context_repeat_count") or 0)
    if repeat:
        messages.append({"role": "user", "content": repeat})
        messages.append({"role": "assistant", "content": "Context noted."})
    messages.append({"role": "user", "content": spec.get("question") or case["prompt"]})
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 256,
    }
    # SparkLab exposes Max as the raw endpoint default, but Prisma's Auto route
    # intentionally uses Low for direct work.  Make that deployment contract
    # explicit so short factual/context cases reach a final answer instead of
    # spending their whole bounded output on hidden reasoning.
    payload["reasoning_effort"] = spec.get("reasoning_effort") or "low"
    if spec.get("tools"):
        payload["tools"] = spec["tools"]
        payload["tool_choice"] = "auto"
    return payload, ledger


def _run_live_case(
    case: dict,
    *,
    endpoint: str,
    fallback_endpoint: str,
    model: str,
    api_key: str,
    timeout: float,
) -> dict:
    from src.provenance import assess_response

    started = time.perf_counter()
    payload, ledger = _live_payload(case, model)
    recovered = False
    last_error = None
    data = None
    wall_seconds = 0.0
    for index, candidate in enumerate([endpoint, fallback_endpoint]):
        if not candidate:
            continue
        try:
            data, elapsed = _post_completion(candidate, payload, api_key=api_key, timeout=timeout)
            wall_seconds += elapsed
            recovered = index > 0
            break
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = exc
    if data is None:
        return _result(
            case,
            False,
            started,
            detail=f"endpoint failure: {type(last_error).__name__}: {last_error}",
            endpoint_recovered=False,
        )

    parsed = _extract_live_response(data, wall_seconds)
    spec = case["live"]
    folded = parsed["content"].casefold()
    contains_ok = all(str(item).casefold() in folded for item in spec.get("expected_contains", []))
    forbidden_ok = all(str(item).casefold() not in folded for item in spec.get("forbidden_contains", []))
    expected_tool = str(spec.get("expected_tool") or "")
    tool_name, tool_arguments = _first_tool(parsed["tool_calls"])
    tool_correct = None
    if expected_tool:
        tool_correct = tool_name == expected_tool and all(
            str(item).casefold() in tool_arguments.casefold()
            for item in spec.get("expected_tool_arguments", [])
        )
    provenance = assess_response(parsed["content"], ledger)
    grounding_status = provenance["grounding_status"] if spec.get("require_grounding") else None
    grounding_ok = not spec.get("require_grounding") or grounding_status == "grounded"
    retrieval_useful = None
    if spec.get("retrieval_usefulness"):
        retrieval_useful = bool((tool_correct is not False) and contains_ok and grounding_ok)
    passed = contains_ok and forbidden_ok and grounding_ok and tool_correct is not False
    detail = (
        f"tool={tool_name or '-'} grounding={grounding_status or '-'} "
        f"contains={contains_ok} forbidden={forbidden_ok} grounding_ok={grounding_ok}"
    )
    return _result(
        case,
        passed,
        started,
        detail=detail,
        tool_correct=tool_correct,
        grounding_status=grounding_status,
        retrieval_useful=retrieval_useful,
        output_tokens=parsed["output_tokens"],
        tokens_per_second=parsed["tokens_per_second"],
        endpoint_recovered=recovered,
    )


def run_live(
    suite: dict,
    *,
    endpoint: str,
    fallback_endpoint: str = "",
    model: str,
    api_key: str = "",
    timeout: float = 180.0,
) -> list[dict]:
    """Run the same fixed cases against an OpenAI-compatible GLM endpoint."""
    if not endpoint:
        raise ValueError("--endpoint or FRONTIER_EVAL_ENDPOINT is required in live mode")
    return [
        _run_live_case(
            case,
            endpoint=endpoint,
            fallback_endpoint=fallback_endpoint,
            model=model,
            api_key=api_key,
            timeout=timeout,
        )
        for case in suite["cases"]
    ]


def summarize(suite: dict, mode: str, results: list[dict]) -> dict:
    """Aggregate capability and efficiency metrics without hiding missing data."""
    count = len(results)
    passed = sum(bool(row.get("passed")) for row in results)

    def fraction(field: str, predicate: Callable[[Any], bool] = bool) -> float | None:
        values = [row[field] for row in results if row.get(field) is not None]
        if not values:
            return None
        return round(sum(predicate(value) for value in values) / len(values), 4)

    wall = [float(row.get("wall_time_ms") or 0) for row in results]
    output_tokens = [row.get("output_tokens") for row in results if row.get("output_tokens") is not None]
    live_tps = [row.get("tokens_per_second") for row in results if row.get("tokens_per_second") is not None]
    grounding_rate = fraction("grounding_status", lambda value: value == "grounded")
    by_dimension = {}
    for row in results:
        bucket = by_dimension.setdefault(row["dimension"], [0, 0])
        bucket[1] += 1
        bucket[0] += int(bool(row["passed"]))
    return {
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "suite_schema_version": suite["schema_version"],
        "suite_id": suite["suite_id"],
        "mode": mode,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "metrics": {
            "case_count": count,
            "passed_count": passed,
            "pass_rate": round(passed / count, 4) if count else 0.0,
            "tool_correctness": fraction("tool_correct"),
            "grounding_rate": grounding_rate,
            "retrieval_usefulness": fraction("retrieval_useful"),
            "median_wall_time_ms": round(statistics.median(wall), 3) if wall else 0.0,
            "p95_wall_time_ms": round(sorted(wall)[max(0, int(len(wall) * 0.95) - 1)], 3) if wall else 0.0,
            "total_wall_time_ms": round(sum(wall), 3),
            "output_tokens": sum(int(value) for value in output_tokens) if output_tokens else None,
            "tokens_per_second": round(statistics.median(live_tps), 3) if live_tps else None,
            "average_turns_to_completion": round(
                statistics.mean(row.get("turns_to_completion", 1) for row in results), 3
            ) if results else 0.0,
            "endpoint_recovery_count": sum(bool(row.get("endpoint_recovered")) for row in results),
            "dimension_pass_rate": {
                key: round(values[0] / values[1], 4)
                for key, values in sorted(by_dimension.items())
            },
        },
        "results": results,
    }


def evaluate_thresholds(summary: dict, thresholds: dict) -> list[str]:
    metrics = summary["metrics"]
    failures = []
    minimums = {
        "min_pass_rate": "pass_rate",
        "min_tool_correctness": "tool_correctness",
        "min_grounding_rate": "grounding_rate",
        "min_retrieval_usefulness": "retrieval_usefulness",
        "min_tokens_per_second": "tokens_per_second",
    }
    for threshold_key, metric_key in minimums.items():
        if threshold_key not in thresholds:
            continue
        value = metrics.get(metric_key)
        # Output token accounting is optional across OpenAI-compatible servers.
        if value is None and metric_key == "tokens_per_second":
            continue
        if value is None or value < thresholds[threshold_key]:
            failures.append(f"{metric_key}={value} below {thresholds[threshold_key]}")
    maximums = {"max_median_wall_time_ms": "median_wall_time_ms"}
    for threshold_key, metric_key in maximums.items():
        if threshold_key in thresholds and metrics[metric_key] > thresholds[threshold_key]:
            failures.append(f"{metric_key}={metrics[metric_key]} above {thresholds[threshold_key]}")
    return failures


def compare_to_baseline(summary: dict, baseline: dict, limits: dict) -> dict:
    """Separate quality regressions from latency/throughput regressions."""
    if baseline.get("suite_id") != summary.get("suite_id"):
        raise ValueError("baseline suite_id does not match current suite")
    current = summary["metrics"]
    previous = baseline.get("metrics") or {}
    capability = []
    latency = []
    capability_limits = {
        "pass_rate": "max_pass_rate_drop",
        "tool_correctness": "max_tool_correctness_drop",
        "grounding_rate": "max_grounding_rate_drop",
        "retrieval_usefulness": "max_retrieval_usefulness_drop",
    }
    for metric, limit_key in capability_limits.items():
        now, before = current.get(metric), previous.get(metric)
        if now is None or before is None:
            continue
        drop = before - now
        if drop > float(limits.get(limit_key, 0.0)):
            capability.append(f"{metric} dropped {drop:.4f} ({before} -> {now})")

    now_wall, before_wall = current.get("median_wall_time_ms"), previous.get("median_wall_time_ms")
    if now_wall is not None and before_wall and before_wall > 0:
        increase = ((now_wall - before_wall) / before_wall) * 100
        if increase > float(limits.get("max_median_wall_time_increase_pct", 20.0)):
            latency.append(f"median_wall_time_ms increased {increase:.1f}% ({before_wall} -> {now_wall})")
    now_tps, before_tps = current.get("tokens_per_second"), previous.get("tokens_per_second")
    if now_tps is not None and before_tps and before_tps > 0:
        drop = ((before_tps - now_tps) / before_tps) * 100
        if drop > float(limits.get("max_tokens_per_second_drop_pct", 20.0)):
            latency.append(f"tokens_per_second dropped {drop:.1f}% ({before_tps} -> {now_tps})")
    return {"capability": capability, "latency": latency}


def _write_json(path: str, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    parser.add_argument("--endpoint", default=os.getenv("FRONTIER_EVAL_ENDPOINT", ""))
    parser.add_argument("--fallback-endpoint", default=os.getenv("FRONTIER_EVAL_FALLBACK_ENDPOINT", ""))
    parser.add_argument("--model", default=os.getenv("FRONTIER_EVAL_MODEL", "glm-5.3-flash"))
    parser.add_argument("--api-key-env", default="FRONTIER_EVAL_API_KEY")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--baseline", default="")
    parser.add_argument("--record-baseline", default="")
    args = parser.parse_args(argv)

    suite = load_suite(args.suite)
    if args.mode == "offline":
        results = run_offline(suite)
    else:
        results = run_live(
            suite,
            endpoint=args.endpoint,
            fallback_endpoint=args.fallback_endpoint,
            model=args.model,
            api_key=os.getenv(args.api_key_env, ""),
            timeout=args.timeout,
        )
    summary = summarize(suite, args.mode, results)
    threshold_failures = evaluate_thresholds(summary, suite["thresholds"][args.mode])
    regressions = {"capability": [], "latency": []}
    if args.baseline:
        with Path(args.baseline).open(encoding="utf-8") as handle:
            regressions = compare_to_baseline(summary, json.load(handle), suite["regression_limits"])
    summary["gate"] = {
        "passed": not threshold_failures and not regressions["capability"] and not regressions["latency"],
        "threshold_failures": threshold_failures,
        "regressions": regressions,
    }
    if args.output:
        _write_json(args.output, summary)
    if args.record_baseline:
        if not summary["gate"]["passed"]:
            print("Refusing to record a failing baseline.", file=sys.stderr)
        else:
            _write_json(args.record_baseline, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
