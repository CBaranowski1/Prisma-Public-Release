"""Strict deployment configuration for an NVIDIA DGX Spark model server.

DGX Spark inference servers such as vLLM expose an OpenAI-compatible API.
This module keeps the environment contract, URL validation, authentication,
and endpoint construction in one place so chat, discovery, diagnostics, and
embeddings cannot silently disagree.
"""

from dataclasses import dataclass
import os
from typing import Mapping, Optional
from urllib.parse import urlsplit, urlunsplit


_TRUE_VALUES = {"1", "true", "yes", "on"}
DGX_SPARK_ENDPOINT_ID = "deployment:dgx-spark"


def _validate_http_url(raw: str, *, field: str) -> str:
    value = (raw or "").strip().rstrip("/")
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} must be a non-empty HTTP(S) URL")

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"{field} must use http or https")
    if not parsed.hostname:
        raise ValueError(f"{field} must include a hostname")
    if parsed.username or parsed.password:
        raise ValueError(f"{field} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field} must not contain a query or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} contains an invalid port") from exc

    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


def normalize_openai_base(raw: str) -> str:
    """Validate a DGX URL and normalize it to an OpenAI ``/v1`` API root."""
    value = _validate_http_url(raw, field="DGX_SPARK_BASE_URL")
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    for suffix in (
        "/chat/completions",
        "/embeddings",
        "/models",
        "/completions",
    ):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    if not path.endswith("/v1"):
        path = f"{path}/v1" if path else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


@dataclass(frozen=True)
class DgxSparkConfig:
    base_url: str
    api_key: str
    model: str
    embedding_url: Optional[str] = None
    embedding_model: str = ""
    max_model_len: Optional[int] = None

    @property
    def headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    @property
    def resolved_embedding_url(self) -> str:
        return self.embedding_url or f"{self.base_url}/embeddings"


def is_dgx_spark_endpoint_id(endpoint_id: Optional[str]) -> bool:
    """Return whether a picker selection targets the managed Spark deployment."""
    return (endpoint_id or "").strip() == DGX_SPARK_ENDPOINT_ID


def is_dgx_spark_url(
    endpoint_url: str,
    config: Optional[DgxSparkConfig] = None,
) -> bool:
    """Match a session URL to the configured Spark API without exposing secrets."""
    if not endpoint_url:
        return False
    try:
        resolved = config or load_dgx_spark_config()
        if resolved is None:
            return False
        return normalize_openai_base(endpoint_url) == resolved.base_url
    except ValueError:
        return False


def load_dgx_spark_config(
    environ: Optional[Mapping[str, str]] = None,
    *,
    require_model: bool = False,
) -> Optional[DgxSparkConfig]:
    """Load the DGX Spark environment contract.

    A configured DGX endpoint requires a bearer token by default. Operators
    serving only on a trusted private loopback/network can explicitly opt out
    with ``DGX_SPARK_ALLOW_INSECURE_NO_AUTH=true``.
    """
    env = environ if environ is not None else os.environ
    raw_base = (env.get("DGX_SPARK_BASE_URL") or "").strip()
    if not raw_base:
        return None

    base_url = normalize_openai_base(raw_base)
    api_key = (env.get("DGX_SPARK_API_KEY") or "").strip()
    allow_no_auth = (
        (env.get("DGX_SPARK_ALLOW_INSECURE_NO_AUTH") or "").strip().lower()
        in _TRUE_VALUES
    )
    if not api_key and not allow_no_auth:
        raise ValueError(
            "DGX_SPARK_API_KEY is required unless "
            "DGX_SPARK_ALLOW_INSECURE_NO_AUTH=true"
        )

    model = (env.get("DGX_SPARK_MODEL") or "").strip()
    if require_model and not model:
        raise ValueError("DGX_SPARK_MODEL is required for chat routing")

    raw_embedding_url = (env.get("DGX_SPARK_EMBEDDING_URL") or "").strip()
    embedding_url = None
    if raw_embedding_url:
        embedding_url = _validate_http_url(
            raw_embedding_url, field="DGX_SPARK_EMBEDDING_URL"
        )

    raw_max_model_len = (env.get("DGX_SPARK_MAX_MODEL_LEN") or "").strip()
    max_model_len = None
    if raw_max_model_len:
        try:
            max_model_len = int(raw_max_model_len)
        except ValueError as exc:
            raise ValueError("DGX_SPARK_MAX_MODEL_LEN must be an integer") from exc
        if not 512 <= max_model_len <= 2_000_000:
            raise ValueError(
                "DGX_SPARK_MAX_MODEL_LEN must be between 512 and 2000000"
            )

    return DgxSparkConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        embedding_url=embedding_url,
        embedding_model=(env.get("DGX_SPARK_EMBEDDING_MODEL") or "").strip(),
        max_model_len=max_model_len,
    )
