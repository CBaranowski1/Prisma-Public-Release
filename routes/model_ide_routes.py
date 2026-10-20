"""Database-inspection endpoints for Prisma's integrated Model IDE.

The retired query-builder UI is intentionally not re-registered. These routes
provide a narrow, read-only schema surface for coding and migration planning.
They never return credentials, table rows, or execute model-generated SQL.
"""

import asyncio
import hashlib
import json
import logging
import re
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import inspect

from core.database import engine, get_db_session
from core.querybuilder_models import DataSource
from src.auth_helpers import owner_filter, require_privilege, require_scope
from src.datasource_connectors import ConnectorError, get_connector
from src.dgx_spark import load_dgx_spark_config
from src.endpoint_resolver import build_chat_url, normalize_base
from src.code_publish_gate import (
    MODEL_IDE_PUBLICATION_POLICY,
    evaluate_model_ide_proposal,
    repair_instruction,
)


INTERNAL_DATABASE_ID = "prisma:internal"
# Leaves room in the managed 32K Spark context for bounded brain grounding and
# an 8K-token complete-file response.
MAX_PROMPT_CHARACTERS = 64_000
logger = logging.getLogger(__name__)


class ModelIdeMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=65_000)


class ModelIdeCompletionRequest(BaseModel):
    model: str = Field(min_length=1, max_length=300)
    messages: list[ModelIdeMessage] = Field(min_length=1, max_length=12)
    temperature: float = Field(default=0.2, ge=0, le=1)
    max_tokens: int = Field(default=8192, ge=1, le=8192)
    workspace_path: str = Field(default="", max_length=500)
    language: str = Field(default="", max_length=50)
    use_brain: bool = True


def _last_user_text(messages: list[ModelIdeMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def _request_only(text: str) -> str:
    return (text or "").split("<untrusted_code>", 1)[0].strip()


def _original_code(text: str) -> str:
    match = re.search(
        r"<untrusted_code>\s*\n?([\s\S]*?)\n?</untrusted_code>",
        text or "",
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _model_ide_user(request: Request) -> str:
    require_scope(request, "chat")
    return require_privilege(
        request,
        "can_manage_data_sources",
        fail_closed=True,
    )


def _owned_source(db, source_id: str, user: str):
    query = db.query(DataSource).filter(DataSource.id == source_id)
    source = owner_filter(query, DataSource, user).first()
    if not source:
        raise HTTPException(404, "Database connection not found")
    return source


def _safe_source_dict(source: DataSource) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "kind": "external",
        "source_type": source.source_type,
        "host": source.host,
        "port": source.port,
        "database": source.database,
        "enabled": bool(source.enabled),
        "has_credentials": bool(source.username or source.password),
        "last_sync_status": source.last_sync_status,
    }


def setup_model_ide_routes() -> APIRouter:
    router = APIRouter(prefix="/api/model-ide", tags=["model_ide"])

    @router.post("/complete")
    async def complete_code_change(
        request: Request,
        payload: ModelIdeCompletionRequest,
    ):
        """Send a bounded IDE request through a model endpoint visible to this user."""
        user = _model_ide_user(request)
        if sum(len(message.content) for message in payload.messages) > MAX_PROMPT_CHARACTERS:
            raise HTTPException(413, "Model IDE prompt is too large")
        last_user = _last_user_text(payload.messages)
        request_text = _request_only(last_user)
        original = _original_code(last_user)
        registry = getattr(request.app.state, "memory_provider_registry", None)

        brain_messages: list[dict] = []
        brain_used: list[dict] = []
        learned_rules: list[str] = []
        if payload.use_brain and registry is not None:
            try:
                from src.brain_context import ranked_brain_context

                brain_messages, brain_used = await ranked_brain_context(
                    registry,
                    request_text or last_user,
                    owner=user,
                    character_budget=8_000,
                )
                from src.conversation_index import extract_code_rules

                learned_rules = extract_code_rules(
                    "\n".join(
                        str(message.get("content", ""))
                        for message in brain_messages
                    )
                )
            except Exception:
                brain_messages, brain_used = [], []

        model_messages = [
            {"role": "system", "content": MODEL_IDE_PUBLICATION_POLICY},
            *brain_messages,
            *[message.model_dump() for message in payload.messages],
        ]

        # Keep endpoint credentials and the desktop bridge token server-side.
        from routes.openai_compat_routes import (
            _detect_provider,
            _proxy_json,
            _resolve_model,
        )

        try:
            spark_deployment = False
            try:
                upstream, model_id, upstream_headers = _resolve_model(
                    payload.model,
                    user,
                )
            except ValueError as configured_endpoint_error:
                # The Spark picker is a server-controlled virtual endpoint, not
                # a ModelEndpoint database row. Resolve that exact active model
                # through the same deployment contract used by chat sessions.
                deployment = load_dgx_spark_config(require_model=True)
                if deployment is None or payload.model != deployment.model:
                    raise configured_endpoint_error
                upstream = deployment.base_url
                model_id = deployment.model
                upstream_headers = deployment.headers
                spark_deployment = True
            base = normalize_base(upstream)
            if _detect_provider(base) == "anthropic":
                raise HTTPException(
                    400,
                    "This Model IDE version requires an OpenAI-compatible endpoint",
                )
            fast_qwen_options = (
                {"chat_template_kwargs": {"enable_thinking": False}}
                if spark_deployment and "qwen" in model_id.casefold()
                else {}
            )
            result = await _proxy_json(
                build_chat_url(base),
                upstream_headers,
                {
                    "model": model_id,
                    "messages": model_messages,
                    "temperature": payload.temperature,
                    "max_tokens": payload.max_tokens,
                    "stream": False,
                    **fast_qwen_options,
                },
                timeout=120,
            )

            raw_value = (
                result.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            # Reasoning-capable models may return ``content: null`` when the
            # generation budget ends during their private reasoning phase.
            # Treat that as an invalid proposal for the publication gate; do
            # not expose or persist the model's separate reasoning field.
            raw = raw_value if isinstance(raw_value, str) else ""
            proposal, gate = evaluate_model_ide_proposal(
                raw,
                original=original,
                language=payload.language,
                workspace_path=payload.workspace_path,
                request_text=request_text,
                learned_rules=learned_rules,
            )
            repair_attempts = 0
            if not gate["passed"]:
                repair_attempts = 1
                repaired_result = await _proxy_json(
                    build_chat_url(base),
                    upstream_headers,
                    {
                        "model": model_id,
                        "messages": [
                            *model_messages,
                            {"role": "assistant", "content": raw[:40_000]},
                            {"role": "user", "content": repair_instruction(gate)},
                        ],
                            "temperature": min(payload.temperature, 0.1),
                            "max_tokens": payload.max_tokens,
                            "stream": False,
                            **fast_qwen_options,
                        },
                    timeout=120,
                )
                repaired_raw_value = (
                    repaired_result.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                repaired_raw = (
                    repaired_raw_value
                    if isinstance(repaired_raw_value, str)
                    else ""
                )
                repaired_proposal, repaired_gate = evaluate_model_ide_proposal(
                    repaired_raw,
                    original=original,
                    language=payload.language,
                    workspace_path=payload.workspace_path,
                    request_text=request_text,
                    learned_rules=learned_rules,
                )
                if repaired_gate["passed"]:
                    result = repaired_result
                    proposal, gate = repaired_proposal, repaired_gate
                else:
                    gate = repaired_gate

            gate["repair_attempts"] = repair_attempts
            gate["brain_notes"] = len(brain_used)
            for choice in result.get("choices", []):
                message = choice.get("message", {})
                if isinstance(message, dict):
                    message.pop("reasoning", None)
                    message.pop("reasoning_content", None)
            result["choices"][0]["message"]["content"] = json.dumps(
                proposal,
                ensure_ascii=False,
            )
            result["prisma_gate"] = gate
            result["brain_used"] = brain_used

            if registry is not None:
                from src.conversation_index import index_exchange_async

                session_hash = hashlib.sha256(
                    f"{user}\0{payload.workspace_path}".encode("utf-8")
                ).hexdigest()[:20]
                asyncio.create_task(index_exchange_async(
                    last_user,
                    result["choices"][0]["message"]["content"],
                    session_id=f"model-ide:{session_hash}",
                    owner=user,
                    model=model_id,
                    surface="model_ide",
                    evaluation=gate,
                ))
        except HTTPException:
            raise
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        except httpx.HTTPStatusError as error:
            raise HTTPException(
                502,
                f"Model endpoint returned HTTP {error.response.status_code}",
            ) from error
        except Exception as error:
            logger.exception("Model IDE completion failed")
            raise HTTPException(502, "Model endpoint could not be reached") from error
        return result

    @router.get("/databases")
    def list_databases(request: Request):
        user = _model_ide_user(request)
        with get_db_session() as db:
            query = owner_filter(db.query(DataSource), DataSource, user)
            external = [_safe_source_dict(source) for source in query.all()]
        return {
            "items": [
                {
                    "id": INTERNAL_DATABASE_ID,
                    "name": "Prisma application database",
                    "kind": "internal",
                    "source_type": "sqlite",
                    "host": None,
                    "port": None,
                    "database": "Prisma runtime data",
                    "enabled": True,
                    "has_credentials": False,
                    "last_sync_status": None,
                },
                *external,
            ],
            "mode": "read_only",
            "migration_policy": (
                "Models may draft migration files, but database changes require "
                "an explicit reviewed migration outside this read-only API."
            ),
        }

    @router.get("/databases/{source_id}/tables")
    def list_database_tables(request: Request, source_id: str):
        user = _model_ide_user(request)
        if source_id == INTERNAL_DATABASE_ID:
            db_inspector = inspect(engine)
            tables = []
            for table_name in sorted(db_inspector.get_table_names()):
                columns = [
                    {
                        "name": column["name"],
                        "type": str(column["type"]),
                        "nullable": bool(column.get("nullable", True)),
                        "primary_key": bool(column.get("primary_key", False)),
                    }
                    for column in db_inspector.get_columns(table_name)
                ]
                tables.append({"name": table_name, "columns": columns})
            return {"database_id": source_id, "tables": tables, "read_only": True}

        with get_db_session() as db:
            source = _owned_source(db, source_id, user)
            try:
                table_names = sorted(get_connector(source).list_tables())
            except ConnectorError as error:
                raise HTTPException(400, str(error)) from error
        return {
            "database_id": source_id,
            "tables": [{"name": name, "columns": []} for name in table_names],
            "read_only": True,
        }

    @router.post("/databases/{source_id}/test")
    def test_database_connection(request: Request, source_id: str):
        user = _model_ide_user(request)
        if source_id == INTERNAL_DATABASE_ID:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            return {"ok": True, "database_id": source_id}

        with get_db_session() as db:
            source = _owned_source(db, source_id, user)
            try:
                get_connector(source).test_connection()
            except ConnectorError as error:
                raise HTTPException(400, str(error)) from error
        return {"ok": True, "database_id": source_id}

    return router
