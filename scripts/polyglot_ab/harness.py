"""Execution-scored polyglot proficiency harness (Ruby + Postgres).

For each problem, ask a model for a solution, score it by real execution, and
write one JSONL result. Supported model transports are selected with
``--backend``: ``ollama``, ``openai``, ``app``, ``llamacpp``, and ``mock``.

OpenAI-compatible authentication is credential-safe by construction: the CLI
accepts only the *name* of an environment variable, never a token value. Run
rows contain a configuration fingerprint and safe metadata, but no endpoint,
header, or credential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.request
from urllib.parse import urlsplit

from runners import score

PROMPT_TMPL = {
    "ruby": (
        "You are an expert Ruby developer. Solve the task. Respond with ONLY "
        "one ```ruby code block defining exactly what is asked, no prose."
        "\n\nTASK:\n{p}\n"
    ),
    "postgres": (
        "You are an expert PostgreSQL developer. Respond with ONLY one ```sql "
        "code block containing a single SELECT that answers the task. No prose, "
        "no DDL.\n\nTASK:\n{p}\n"
    ),
}

OPENAI_API_KEY_ENV = "POLYGLOT_OPENAI_API_KEY"
OPENAI_BASE_URL_ENV = "POLYGLOT_OPENAI_BASE_URL"
REASONING_EFFORTS = ("low", "medium", "high", "max")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validated_http_base(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "model endpoint must be an http(s) URL without credentials, "
            "query parameters, or fragments"
        )
    return value.rstrip("/")


def openai_chat_url(base_url: str) -> str:
    """Normalize a host, ``/v1`` base, or full chat-completions URL."""
    base = _validated_http_base(base_url)
    path = urlsplit(base).path.rstrip("/")
    if path.endswith("/chat/completions"):
        return base
    if path.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _api_key_from_env(api_key_env: str) -> str:
    if not _ENV_NAME.fullmatch(api_key_env):
        raise ValueError("API-key environment variable name is invalid")
    return os.getenv(api_key_env, "").strip()


def call_ollama(model: str, prompt: str, temperature: float) -> str:
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }).encode()
    host = _validated_http_base(os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    request = urllib.request.Request(
        host + "/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:  # nosec B310
        return json.loads(response.read())["response"]


def call_openai(
    model: str,
    prompt: str,
    temperature: float,
    *,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    api_key_env: str = OPENAI_API_KEY_ENV,
) -> str:
    """Call an OpenAI-compatible endpoint without accepting a raw CLI key."""
    if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"unsupported reasoning effort: {reasoning_effort}")

    configured_base = base_url or os.getenv(
        OPENAI_BASE_URL_ENV, "http://127.0.0.1:1919/v1"
    )
    endpoint = openai_chat_url(configured_base)
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort

    headers = {"Content-Type": "application/json"}
    api_key = _api_key_from_env(api_key_env)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=1200) as response:  # nosec B310
        result = json.loads(response.read())

    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("OpenAI-compatible response has no message content") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("OpenAI-compatible response message content is empty")
    return content


def call_app(model: str, prompt: str, temperature: float) -> str:
    del temperature
    body = json.dumps({"message": prompt, "model": model}).encode()
    url = _validated_http_base(os.getenv("PRISM_URL", "http://localhost:7000"))
    token = os.getenv("PRISM_API_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url + "/api/v1/chat", data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=1200) as response:  # nosec B310
        data = json.loads(response.read())
    return data.get("reply") or data.get("response") or data.get("content", "")


_LLM = None


def call_llamacpp(model_path: str, prompt: str, temperature: float) -> str:
    global _LLM
    from llama_cpp import Llama

    if _LLM is None:
        _LLM = Llama(
            model_path=model_path,
            n_ctx=2048,
            n_threads=os.cpu_count(),
            verbose=False,
        )
    out = _LLM.create_chat_completion(
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        max_tokens=512,
    )
    return out["choices"][0]["message"]["content"]


def call_model(
    backend: str,
    model: str,
    prompt: str,
    temperature: float,
    *,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    api_key_env: str = OPENAI_API_KEY_ENV,
) -> str:
    """Backend-neutral generation seam shared by runs and screening."""
    if backend == "ollama":
        return call_ollama(model, prompt, temperature)
    if backend == "openai":
        return call_openai(
            model,
            prompt,
            temperature,
            base_url=base_url,
            reasoning_effort=reasoning_effort,
            api_key_env=api_key_env,
        )
    if backend == "app":
        return call_app(model, prompt, temperature)
    if backend == "llamacpp":
        return call_llamacpp(model, prompt, temperature)
    if backend == "mock":
        return ""
    raise ValueError(f"unsupported backend: {backend}")


def run_fingerprint(
    *,
    backend: str,
    model: str,
    arm: str,
    temperature: float,
    sql_backend: str,
    reasoning_effort: str | None,
    base_url: str | None,
) -> str:
    """Hash safe run identity; the raw endpoint is never stored in rows."""
    endpoint = ""
    if backend == "openai":
        configured_base = base_url or os.getenv(
            OPENAI_BASE_URL_ENV, "http://127.0.0.1:1919/v1"
        )
        endpoint = openai_chat_url(configured_base)
    identity = {
        "arm": arm,
        "backend": backend,
        "endpoint": endpoint,
        "model": model,
        "reasoning_effort": reasoning_effort or "",
        "sql_backend": sql_backend,
        "temperature": temperature,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _safe_error(exc: Exception, secret: str) -> str:
    message = f"{type(exc).__name__}: {str(exc)[:200]}"
    return message.replace(secret, "[redacted]") if secret else message


def run(
    dataset: str,
    backend: str,
    model: str,
    arm: str,
    out_path: str,
    temperature: float,
    sql_backend: str,
    limit: int,
    *,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    api_key_env: str = OPENAI_API_KEY_ENV,
) -> tuple[int, int, int]:
    with open(dataset, encoding="utf-8") as dataset_file:
        problems = [json.loads(line) for line in dataset_file if line.strip()]
    if limit:
        problems = problems[:limit]

    fingerprint = run_fingerprint(
        backend=backend,
        model=model,
        arm=arm,
        temperature=temperature,
        sql_backend=sql_backend,
        reasoning_effort=reasoning_effort,
        base_url=base_url,
    )
    valid_ids = {problem["task_id"] for problem in problems}
    existing: dict[str, str] = {}
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as result_file:
            for line in result_file:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if (
                    row.get("task_id") in valid_ids
                    and row.get("run_fingerprint") == fingerprint
                ):
                    existing[row["task_id"]] = line.rstrip("\n")

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as result_file:
        for problem in problems:
            if problem["task_id"] in existing:
                result_file.write(existing[problem["task_id"]] + "\n")

    done = set(existing)
    passed_count = 0
    scored_count = 0
    error_count = 0
    secret = _api_key_from_env(api_key_env) if backend == "openai" else ""
    with open(out_path, "a", encoding="utf-8") as result_file:
        for problem in problems:
            if problem["task_id"] in done:
                continue
            prompt = PROMPT_TMPL[problem["language"]].format(p=problem["prompt"])
            model_error = None
            try:
                text = call_model(
                    backend,
                    model,
                    prompt,
                    temperature,
                    base_url=base_url,
                    reasoning_effort=reasoning_effort,
                    api_key_env=api_key_env,
                )
                if backend == "mock":
                    text = "```\n" + problem["reference"] + "\n```"
            except Exception as exc:
                text = ""
                model_error = _safe_error(exc, secret)
            passed, detail = score(text, problem, sql_backend=sql_backend)
            scored_count += 1
            passed_count += int(bool(passed))
            error_count += int(model_error is not None)
            row = {
                "task_id": problem["task_id"],
                "language": problem["language"],
                "passed": bool(passed),
                "arm": arm,
                "backend": backend,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "run_fingerprint": fingerprint,
            }
            if model_error:
                row["model_error"] = model_error
            result_file.write(json.dumps(row) + "\n")
            result_file.flush()
            tag = "ERR " if model_error else ("PASS" if passed else "FAIL")
            suffix = "" if passed else f"  ({(model_error or detail)[:80]})"
            print(f"  [{tag}] {problem['task_id']}{suffix}")

    print(
        f"  arm={arm} backend={backend} model={model} "
        f"reasoning={reasoning_effort or 'default'}: "
        f"{passed_count}/{scored_count} pass@1 ({error_count} model errors)"
    )
    return passed_count, scored_count, error_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument(
        "--backend",
        required=True,
        choices=["ollama", "openai", "app", "llamacpp", "mock"],
    )
    parser.add_argument("--model", default="llama3.1:8b")
    parser.add_argument("--arm", default="baseline")
    parser.add_argument("--out", required=True)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--sql-backend", default="postgres", choices=["postgres", "sqlite"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--base-url",
        help="OpenAI-compatible host, /v1 base, or full chat-completions URL",
    )
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS)
    parser.add_argument(
        "--api-key-env",
        default=OPENAI_API_KEY_ENV,
        help="name of the environment variable containing the API key",
    )
    args = parser.parse_args()
    run(
        args.dataset,
        args.backend,
        args.model,
        args.arm,
        args.out,
        args.temperature,
        args.sql_backend,
        args.limit,
        base_url=args.base_url,
        reasoning_effort=args.reasoning_effort,
        api_key_env=args.api_key_env,
    )


if __name__ == "__main__":
    main()
