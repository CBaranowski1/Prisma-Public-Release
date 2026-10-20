"""Memory provider interfaces for native and external memory systems."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# GRAY-BRN-05: process-level "warned once" latch, copying the exact shape of
# src/embeddings.py::_http_embed_down. Fires exactly one WARNING for the rest
# of the process the first time NativeMemoryProvider.recall() falls through
# to the lexical path because the vector path was genuinely unavailable,
# unhealthy, or raised mid-process -- never on a merely-empty vector result
# (empty != degraded).
_lexical_fallback_warned = False


def _warn_lexical_fallback_once() -> None:
    """Fire exactly one WARNING per process the first time recall degrades to
    the lexical-only fallback because the vector path was genuinely
    unavailable, unhealthy, or raised mid-process (GRAY-BRN-05). Must NOT be
    called when the vector path was healthy but simply returned zero
    matches -- empty is not degraded."""
    global _lexical_fallback_warned
    if not _lexical_fallback_warned:
        _lexical_fallback_warned = True
        logger.warning(
            "Native memory recall degraded to lexical-only fallback "
            "(embedder/vector store absent or failed); recall quality is "
            "reduced for the rest of this process"
        )


@dataclass
class MemoryRecord:
    """Provider-neutral memory entry."""

    id: str
    text: str
    timestamp: int = 0
    category: str = "fact"
    source: str = "unknown"
    owner: Optional[str] = None
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemorySearchHit:
    """A memory returned by provider recall."""

    memory: MemoryRecord
    provider_id: str
    score: Optional[float] = None
    # GRAY-BRN-05: "vector" | "lexical" -- which recall path produced this hit.
    mode: str = "vector"
    # GRAY-BRN-13: True for hits from the degraded lexical-fallback path.
    low_confidence: bool = False


@dataclass
class BrainDoc:
    """A document entry surfaced in the Brain tab."""

    id: str
    title: str
    snippet: str
    category: str = ""
    source_path: str = ""
    score: float = 0.0


class MemoryProvider(ABC):
    """Base contract for Prisma memory providers.

    The native memory provider should always be available. External providers
    can add recall/write behavior and their own tools without replacing the
    built-in local memory baseline.
    """

    provider_id = "unknown"
    display_name = "Unknown"
    enabled = True
    writable = True

    async def initialize(self) -> None:
        """Prepare provider resources before use."""

    async def shutdown(self) -> None:
        """Release provider resources."""

    @abstractmethod
    async def remember(
        self,
        text: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        category: str = "fact",
        source: str = "user",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryRecord:
        """Store a memory and return the stored record."""

    @abstractmethod
    async def recall(
        self,
        query: str,
        *,
        owner: Optional[str] = None,
        top_k: int = 5,
    ) -> List[MemorySearchHit]:
        """Return provider memories relevant to the query."""

    @abstractmethod
    async def list_memories(
        self,
        *,
        owner: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryRecord]:
        """List memories visible to the owner."""

    @abstractmethod
    async def delete(self, memory_id: str, *, owner: Optional[str] = None) -> bool:
        """Delete a memory by ID when allowed by the provider."""

    async def search(
        self,
        query: str,
        *,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> "List[BrainDoc]":
        """Optional: power the Brain tab. Default = no browse support."""
        return []

    async def categories(self) -> List[str]:
        """Optional: category filter chips for the Brain tab."""
        return []

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return provider-defined tool schemas when this provider is enabled."""
        return []

    async def handle_tool_call(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Handle a provider-defined tool call."""
        raise KeyError(f"Provider {self.provider_id} does not expose tool {name}")


class NativeMemoryProvider(MemoryProvider):
    """Provider adapter for Prisma's built-in memory manager and vector store."""

    provider_id = "native"
    display_name = "Prisma native memory"

    _CORE_FIELDS = {
        "id",
        "text",
        "timestamp",
        "source",
        "category",
        "uses",
        "owner",
        "session_id",
        "metadata",
    }

    def __init__(self, memory_manager, memory_vector=None):
        self.memory_manager = memory_manager
        self.memory_vector = memory_vector

    def _to_record(self, entry: Dict[str, Any]) -> MemoryRecord:
        metadata = {
            key: value
            for key, value in entry.items()
            if key not in self._CORE_FIELDS
        }
        stored_metadata = entry.get("metadata")
        if isinstance(stored_metadata, dict):
            metadata.update(stored_metadata)

        return MemoryRecord(
            id=entry.get("id", ""),
            text=entry.get("text", ""),
            timestamp=entry.get("timestamp", 0),
            category=entry.get("category", "fact"),
            source=entry.get("source", "unknown"),
            owner=entry.get("owner"),
            session_id=entry.get("session_id"),
            metadata=metadata,
        )

    async def remember(
        self,
        text: str,
        *,
        owner: Optional[str] = None,
        session_id: Optional[str] = None,
        category: str = "fact",
        source: str = "user",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryRecord:
        entry = self.memory_manager.add_entry(
            text,
            source=source,
            category=category,
            owner=owner,
        )
        # GRAY-BRN-08: funnel through the guarded add_and_save() so the full
        # load->append->save span is lock-protected (not just save()).
        entry = self.memory_manager.add_and_save(
            entry=entry, session_id=session_id, metadata=metadata,
        )

        if self._vector_available():
            self.memory_vector.add(entry["id"], entry["text"])

        # GRAY-BRN-06: opportunistic, best-effort bounded eviction. compact()
        # is flag-gated (brain_retention_enabled, default OFF) and never
        # raises, so this is an inert no-op by default and can never fail
        # the remember() call. When enabled, evicted ids are vector-removed
        # too, keeping Chroma in sync with memory.json (the authoritative
        # store).
        try:
            evicted_ids = self.memory_manager.compact()
        except Exception:
            evicted_ids = []
        if evicted_ids and self._vector_available():
            for evicted_id in evicted_ids:
                try:
                    self.memory_vector.remove(evicted_id)
                except Exception:
                    pass

        return self._to_record(entry)

    async def recall(
        self,
        query: str,
        *,
        owner: Optional[str] = None,
        top_k: int = 5,
    ) -> List[MemorySearchHit]:
        memories = self.memory_manager.load(owner=owner)
        by_id = {m.get("id"): m for m in memories}

        # GRAY-BRN-05: track whether reaching the lexical fallback below is a
        # genuine degradation (no vector store / unhealthy / mid-process
        # search failure) vs. a healthy vector store that simply found zero
        # matches -- only the former fires the WARN-once (empty != degraded).
        degraded = not self._vector_available()

        if self._vector_available():
            # GRAY-BRN-05 Pitfall 4: MemoryVectorStore.healthy is an
            # init-time-only snapshot -- a mid-process embedder failure
            # inside .search() must degrade gracefully to lexical instead of
            # raising uncaught out of recall().
            try:
                vector_results = self.memory_vector.search(query, k=top_k)
            except Exception as e:
                degraded = True
                logger.debug(
                    "[memory-provider] vector search failed mid-process, "
                    "falling back to lexical recall: %s", e, exc_info=True,
                )
                vector_results = None

            if vector_results is not None:
                hits: List[MemorySearchHit] = []
                for result in vector_results:
                    if not isinstance(result, dict):
                        continue
                    memory_id = result.get("memory_id")
                    entry = by_id.get(memory_id) if memory_id else result
                    if not entry:
                        continue
                    if owner is not None and entry.get("owner") != owner:
                        continue
                    hits.append(
                        MemorySearchHit(
                            memory=self._to_record(entry),
                            provider_id=self.provider_id,
                            score=result.get("score"),
                        )
                    )
                if hits:
                    return hits

        if degraded:
            _warn_lexical_fallback_once()

        fallback = self.memory_manager.get_relevant_memories(
            query,
            memories,
            max_items=top_k,
        )
        return [
            MemorySearchHit(
                memory=self._to_record(entry),
                provider_id=self.provider_id,
                score=None,
                mode="lexical",
                low_confidence=True,
            )
            for entry in fallback
        ]

    async def list_memories(
        self,
        *,
        owner: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryRecord]:
        return [
            self._to_record(entry)
            for entry in self.memory_manager.load(owner=owner)[:limit]
        ]

    async def delete(self, memory_id: str, *, owner: Optional[str] = None) -> bool:
        # GRAY-BRN-08: funnel through the guarded delete_by_id() so the full
        # load->filter->save span is lock-protected (not just save()).
        removed = self.memory_manager.delete_by_id(memory_id, owner=owner)
        if removed is None:
            return False

        if self._vector_available():
            self.memory_vector.remove(removed.get("id", memory_id))
        return True

    def _vector_available(self) -> bool:
        return bool(self.memory_vector and getattr(self.memory_vector, "healthy", True))


class MemoryProviderRegistry:
    """Container for native and optional external memory providers."""

    def __init__(self, providers: Optional[Iterable[MemoryProvider]] = None):
        self._providers: Dict[str, MemoryProvider] = {}
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: MemoryProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"Memory provider already registered: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> MemoryProvider:
        return self._providers[provider_id]

    def all(self) -> List[MemoryProvider]:
        return list(self._providers.values())

    def active(self) -> List[MemoryProvider]:
        return [provider for provider in self._providers.values() if provider.enabled]

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas: List[Dict[str, Any]] = []
        seen: Dict[str, str] = {}

        for provider in self.active():
            for schema in provider.get_tool_schemas():
                name = self._tool_name(schema)
                if name in seen:
                    raise ValueError(
                        f"Memory tool name conflict: {name} from "
                        f"{provider.provider_id} already exposed by {seen[name]}"
                    )
                seen[name] = provider.provider_id
                schemas.append(schema)

        return schemas

    async def handle_tool_call(self, name: str, arguments: Dict[str, Any]) -> Any:
        provider_by_tool: Dict[str, MemoryProvider] = {}
        for provider in self.active():
            for schema in provider.get_tool_schemas():
                tool_name = self._tool_name(schema)
                if tool_name in provider_by_tool:
                    raise ValueError(
                        f"Memory tool name conflict: {tool_name} from "
                        f"{provider.provider_id} already exposed by "
                        f"{provider_by_tool[tool_name].provider_id}"
                    )
                provider_by_tool[tool_name] = provider

        provider = provider_by_tool.get(name)
        if provider:
            return await provider.handle_tool_call(name, arguments)
        raise KeyError(f"No active memory provider exposes tool {name}")

    @staticmethod
    def _tool_name(schema: Dict[str, Any]) -> str:
        if not isinstance(schema, dict):
            raise ValueError("Memory provider tool schema must be a dict")
        name = schema.get("name")
        if isinstance(name, str) and name:
            return name
        function = schema.get("function")
        if isinstance(function, dict):
            function_name = function.get("name")
            if isinstance(function_name, str) and function_name:
                return function_name
        raise ValueError("Memory provider tool schema is missing a tool name")
