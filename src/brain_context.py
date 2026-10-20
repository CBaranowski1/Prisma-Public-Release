"""Shared, bounded brain retrieval for Chat and Model IDE."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any

from src.prompt_security import untrusted_context_message

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    return max(0, min(value, maximum))


DEFAULT_TOP_K = _env_int(
    "PRISMA_BRAIN_CONTEXT_TOP_K",
    3,
    maximum=20,
)
DEFAULT_CHARACTER_BUDGET = _env_int(
    "PRISMA_BRAIN_CONTEXT_CHARACTER_BUDGET",
    12_000,
    maximum=50_000,
)
PER_NOTE_CHARACTER_CAP = 5_000


def _clip(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    cut = value[: max(0, limit - 1)]
    boundary = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("; "))
    if boundary >= max(100, int(limit * 0.55)):
        cut = cut[: boundary + 1]
    return cut.rstrip() + "…"


async def ranked_brain_context(
    registry,
    query: str,
    *,
    owner: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    character_budget: int = DEFAULT_CHARACTER_BUDGET,
) -> tuple[list[dict], list[dict]]:
    """Return untrusted prompt messages and the exact notes injected.

    Individual providers are queried concurrently. The synthetic ``brain:all``
    provider is skipped because querying it alongside the individual providers
    duplicates every result and makes telemetry disagree with the prompt.
    """
    top_k = min(max(0, int(top_k)), DEFAULT_TOP_K)
    if registry is None or not (query or "").strip() or top_k <= 0:
        return [], []

    try:
        providers = [
            provider
            for provider in registry.active()
            if getattr(provider, "provider_id", "") not in {"native", "brain:all"}
        ]
    except Exception:
        return [], []
    if not providers:
        return [], []

    started = time.perf_counter()

    async def _recall(provider):
        try:
            return provider, await provider.recall(
                query,
                owner=owner,
                top_k=max(top_k, 5),
            )
        except Exception as exc:
            logger.debug(
                "Brain recall skipped for %s: %s",
                getattr(provider, "provider_id", "?"),
                exc,
            )
            return provider, []

    batches = await asyncio.gather(*(_recall(provider) for provider in providers))
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider, hits in batches:
        provider_id = getattr(provider, "provider_id", "unknown")
        provider_name = getattr(provider, "display_name", provider_id)
        for rank, hit in enumerate(hits or [], start=1):
            memory = getattr(hit, "memory", None)
            text = (getattr(memory, "text", "") or "").strip()
            if not text:
                continue
            normalized = " ".join(text.lower().split())
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            raw_score = getattr(hit, "score", None)
            score = float(raw_score) if isinstance(raw_score, (int, float)) else 0.0
            candidates.append(
                {
                    "provider": provider_id,
                    "provider_name": provider_name,
                    "id": getattr(memory, "id", "") or digest[:16],
                    "category": getattr(memory, "category", "") or "",
                    "source": getattr(memory, "source", "") or provider_id,
                    "text": text,
                    "score": score,
                    "provider_rank": rank,
                }
            )

    candidates.sort(
        key=lambda item: (item["score"], -item["provider_rank"]),
        reverse=True,
    )

    messages: list[dict] = []
    used: list[dict] = []
    remaining = max(0, int(character_budget))
    for candidate in candidates[:top_k]:
        if remaining <= 0:
            break
        note = _clip(candidate["text"], min(PER_NOTE_CHARACTER_CAP, remaining))
        if not note:
            continue
        messages.append(
            untrusted_context_message(
                f"brain memory from {candidate['provider_name']}",
                note,
            )
        )
        used.append(
            {
                "provider": candidate["provider"],
                "provider_name": candidate["provider_name"],
                "id": candidate["id"],
                "category": candidate["category"],
                "source": candidate["source"],
                "score": candidate["score"],
                "characters": len(note),
            }
        )
        remaining -= len(note)

    logger.info(
        "brain_context providers=%d candidates=%d injected=%d characters=%d latency_ms=%.1f",
        len(providers),
        len(candidates),
        len(used),
        sum(item["characters"] for item in used),
        (time.perf_counter() - started) * 1000,
    )
    return messages, used
