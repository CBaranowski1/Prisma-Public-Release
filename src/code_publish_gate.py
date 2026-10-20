"""Fast deterministic publication gate for Model IDE code proposals."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from src.brain_privacy import contains_pii_or_secret
from src.council.verify import bash_syntax_check, verify_answer

MODEL_IDE_PUBLICATION_POLICY = """Prisma server-side code publication contract:
Return JSON only with exactly the keys summary, replacement, migration_sql, and migration_name.
Any replacement must be the complete requested file, syntactically valid for the declared language,
and preserve unrelated behavior and public interfaces. Do not introduce unresolved TODO/FIXME
markers, merge-conflict markers, stubs, undefined placeholders, secrets, or credentials unless the
user explicitly requested a scaffold. Validate inputs, handle failures explicitly, use parameterized
database access, and fail closed for authentication/authorization. A migration is a draft only:
never claim it ran and never mix migration SQL into the application-file replacement."""

_ALLOWED_KEYS = {"summary", "replacement", "migration_sql", "migration_name"}
_CONFLICT_MARKERS = re.compile(r"(?m)^(?:<<<<<<<|=======|>>>>>>>)(?: .*)?$")
_PLACEHOLDER_MARKERS = re.compile(
    r"(?im)(?:\bTODO\b|\bFIXME\b|\bNotImplementedError\b|"
    r"raise\s+NotImplemented\b|^\s*pass\s*(?:#.*)?$)"
)
_STUB_REQUEST = re.compile(
    r"\b(?:stub|scaffold|skeleton|placeholder|todo)\b",
    re.IGNORECASE,
)
_EXECUTED_CLAIM = re.compile(
    r"\b(?:migration|sql|database change)\b.{0,45}\b"
    r"(?:executed|applied|ran|completed)\b",
    re.IGNORECASE | re.DOTALL,
)
_SAFE_MIGRATION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.sql$")
_DYNAMIC_SQL = re.compile(
    r"(?is)\b(?:execute|executemany|query)\s*\(\s*(?:f[\"']|"
    r"[\"'][^\"']*[\"']\s*\+|[^)]*\.format\s*\()"
)
_DYNAMIC_EXEC = re.compile(r"\b(?:eval|exec)\s*\(")
_UNSAFE_SHELL = re.compile(
    r"\bshell\s*=\s*True\b|\bos\.system\s*\(",
    re.IGNORECASE,
)


def _extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start = text.find("{")
        if start < 0:
            raise ValueError("response did not contain a JSON object")
        try:
            value, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ValueError(f"response JSON is invalid: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("response JSON must be an object")
    return value


def _language(language: str, workspace_path: str) -> str:
    value = (language or "").strip().lower()
    aliases = {
        "py": "python",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "sh": "shell",
        "bash": "shell",
        "yml": "yaml",
    }
    if value and value not in {"plaintext", "text", "unknown"}:
        return aliases.get(value, value)
    suffix = Path(workspace_path or "").suffix.lower()
    return {
        ".py": "python",
        ".json": "json",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".sh": "shell",
        ".bash": "shell",
        ".sql": "sql",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(suffix, "unknown")


def _node_syntax_check(code: str) -> tuple[bool | None, str]:
    path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".mjs",
            delete=False,
        ) as handle:
            handle.write(code)
            path = handle.name
        env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "PATHEXT", "TEMP", "TMP")
            if key in os.environ
        }
        result = subprocess.run(
            ["node", "--check", path],
            capture_output=True,
            text=True,
            timeout=8,
            env=env,
        )
        if result.returncode == 0:
            return True, ""
        detail = (result.stderr or result.stdout or "JavaScript syntax check failed").strip()
        return False, detail.splitlines()[-1][:400]
    except FileNotFoundError:
        return None, "node is not available"
    except subprocess.TimeoutExpired:
        return False, "JavaScript syntax check timed out"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _syntax_check(code: str, language: str) -> tuple[bool | None, str]:
    try:
        if language == "python":
            ast.parse(code)
            return True, ""
        if language == "json":
            json.loads(code)
            return True, ""
        if language == "javascript":
            return _node_syntax_check(code)
        if language == "shell":
            return bash_syntax_check(code)
        if language == "sql":
            verdict = verify_answer(f"```sql\n{code}\n```", execute=False)
            if verdict.get("verified"):
                return bool(verdict.get("passed")), verdict.get("detail", "")
            return None, "a genuine SQL parser is not installed"
        if language == "yaml":
            try:
                import yaml
            except ImportError:
                return None, "PyYAML is not installed"
            yaml.safe_load(code)
            return True, ""
        return None, f"no deterministic validator is configured for {language}"
    except (SyntaxError, json.JSONDecodeError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _typed_optional(value: Any, name: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{name} must be a string or null")
        return None
    return value if value.strip() else None


def _evaluate_learned_rules(
    code: str,
    rules: list[str],
    *,
    contains_sensitive: bool,
) -> tuple[list[dict[str, str]], list[str]]:
    """Measure the subset of historical critique rules with safe local checks."""
    results: list[dict[str, str]] = []
    failures: list[str] = []
    for rule in rules[:16]:
        lowered = rule.lower()
        status = "unmeasured"
        check = "semantic_review_required"
        failed = False
        if "parameteriz" in lowered or "sql injection" in lowered:
            check = "no_dynamic_sql_construction"
            failed = bool(_DYNAMIC_SQL.search(code))
            status = "failed" if failed else "passed"
        elif any(word in lowered for word in ("secret", "credential", "api key")):
            check = "no_secret_shaped_output"
            failed = contains_sensitive
            status = "failed" if failed else "passed"
        elif (
            any(word in lowered for word in ("never", "avoid", "do not", "don't"))
            and any(word in lowered for word in ("eval", "exec"))
        ):
            check = "no_dynamic_eval_or_exec"
            failed = bool(_DYNAMIC_EXEC.search(code))
            status = "failed" if failed else "passed"
        elif (
            any(word in lowered for word in ("shell", "command injection"))
            and any(word in lowered for word in ("never", "avoid", "do not", "don't"))
        ):
            check = "no_unsafe_shell_execution"
            failed = bool(_UNSAFE_SHELL.search(code))
            status = "failed" if failed else "passed"

        results.append({"rule": rule, "status": status, "check": check})
        if failed:
            failures.append(f"learned critique rule failed: {rule[:240]}")
    return results, failures


def evaluate_model_ide_proposal(
    raw: str,
    *,
    original: str = "",
    language: str = "",
    workspace_path: str = "",
    request_text: str = "",
    learned_rules: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize and validate one model proposal without executing its code."""
    started = time.perf_counter()
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []
    try:
        document = _extract_json(raw)
    except ValueError as exc:
        report = {
            "passed": False,
            "verified": False,
            "publishable": False,
            "language": _language(language, workspace_path),
            "checks": [{"name": "response_json", "status": "failed"}],
            "errors": [str(exc)],
            "warnings": [],
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        return {
            "summary": "Prisma blocked an invalid model response.",
            "replacement": None,
            "migration_sql": None,
            "migration_name": None,
        }, report

    extra = sorted(set(document) - _ALLOWED_KEYS)
    if extra:
        errors.append(f"unexpected response keys: {', '.join(extra)}")
    checks.append(
        {"name": "response_json", "status": "passed" if not extra else "failed"}
    )

    summary = document.get("summary", "")
    if not isinstance(summary, str):
        errors.append("summary must be a string")
        summary = ""
    if len(summary) > 4_000:
        errors.append("summary exceeds 4,000 characters")
        summary = summary[:4_000]
    replacement = _typed_optional(document.get("replacement"), "replacement", errors)
    migration_sql = _typed_optional(
        document.get("migration_sql"), "migration_sql", errors
    )
    migration_name = _typed_optional(
        document.get("migration_name"), "migration_name", errors
    )
    normalized = {
        "summary": summary.strip() or "Model proposal",
        "replacement": replacement,
        "migration_sql": migration_sql,
        "migration_name": migration_name,
    }

    if _EXECUTED_CLAIM.search(normalized["summary"]):
        errors.append("summary claims a database migration was executed")

    resolved_language = _language(language, workspace_path)
    rule_results: list[dict[str, str]] = []
    if replacement is not None:
        if len(replacement) > 120_000:
            errors.append("replacement exceeds 120,000 characters")
        sensitive, reason = contains_pii_or_secret(replacement)
        if sensitive:
            errors.append(f"replacement contains {reason}")
        if _CONFLICT_MARKERS.search(replacement):
            errors.append("replacement contains merge-conflict markers")
        if not _STUB_REQUEST.search(request_text or ""):
            original_placeholders = len(_PLACEHOLDER_MARKERS.findall(original or ""))
            proposed_placeholders = len(_PLACEHOLDER_MARKERS.findall(replacement))
            if proposed_placeholders > original_placeholders:
                errors.append("replacement introduces unfinished placeholder markers")

        syntax_ok, syntax_detail = _syntax_check(replacement, resolved_language)
        if syntax_ok is False:
            errors.append(f"{resolved_language} validation failed: {syntax_detail}")
            checks.append({"name": "syntax", "status": "failed"})
        elif syntax_ok is None:
            warnings.append(syntax_detail)
            checks.append({"name": "syntax", "status": "unverified"})
        else:
            checks.append({"name": "syntax", "status": "passed"})

        rule_results, rule_failures = _evaluate_learned_rules(
            replacement,
            learned_rules or [],
            contains_sensitive=sensitive,
        )
        errors.extend(rule_failures)

    if migration_sql is not None:
        if len(migration_sql) > 60_000:
            errors.append("migration_sql exceeds 60,000 characters")
        sensitive, reason = contains_pii_or_secret(migration_sql)
        if sensitive:
            errors.append(f"migration_sql contains {reason}")
        sql_ok, sql_detail = _syntax_check(migration_sql, "sql")
        if sql_ok is False:
            errors.append(f"SQL validation failed: {sql_detail}")
        elif sql_ok is None:
            warnings.append(sql_detail)
        if not migration_name or not _SAFE_MIGRATION_NAME.fullmatch(migration_name):
            errors.append("migration_name must be a safe .sql basename")
    elif migration_name is not None:
        errors.append("migration_name was provided without migration_sql")

    passed = not errors
    syntax_unverified = any(
        check["status"] == "unverified" for check in checks
    ) or (migration_sql is not None and any("SQL parser" in item for item in warnings))
    measured_rules = [
        item for item in rule_results if item["status"] in {"passed", "failed"}
    ]
    passed_rules = [item for item in measured_rules if item["status"] == "passed"]
    report = {
        "passed": passed,
        "verified": bool(passed and not syntax_unverified),
        "publishable": passed,
        "language": resolved_language,
        "checks": checks,
        "errors": errors,
        "warnings": sorted(set(warnings)),
        "critique_rules": rule_results,
        "critique_stick_rate": (
            round(len(passed_rules) / len(measured_rules), 4)
            if measured_rules
            else None
        ),
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    if not passed:
        normalized["replacement"] = None
        normalized["migration_sql"] = None
        normalized["migration_name"] = None
    return normalized, report


def repair_instruction(report: dict[str, Any]) -> str:
    errors = "\n".join(f"- {item}" for item in report.get("errors", []))
    return (
        "Your previous proposal was blocked by Prisma's deterministic code gate.\n"
        f"{errors}\n\nReturn one corrected JSON object that obeys the server-side "
        "publication contract. Do not add commentary or Markdown fences."
    )
