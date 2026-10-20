"""Discovery + isolation for drop-in brain providers.

Any .py file in the project `providers/` folder that defines a MemoryProvider
subclass is loaded at startup. Every call is wrapped so a misbehaving provider
degrades to empty/[]-and-unhealthy instead of crashing Prisma.
"""
from __future__ import annotations
import asyncio
import importlib.util
import inspect
import logging
import pathlib
from typing import List, Optional
from src.memory_provider import MemoryProvider, MemorySearchHit, BrainDoc

logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 8.0

class GuardedProvider(MemoryProvider):
    """Wraps a provider; catches exceptions + enforces a per-call timeout."""
    def __init__(self, inner: MemoryProvider, timeout_s: float = DEFAULT_TIMEOUT):
        self._inner = inner; self._t = timeout_s
        self.provider_id = inner.provider_id; self.display_name = inner.display_name
        self.color = getattr(inner, "color", "")
        self.healthy = True; self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        # Proxy the inner provider's enabled state LIVE (it may be a property that
        # depends on settings) instead of snapshotting it at construction time.
        return bool(getattr(self._inner, "enabled", True))

    @property
    def writable(self) -> bool:
        # Proxy the inner provider's writable state LIVE, same rationale as enabled.
        return bool(getattr(self._inner, "writable", True))

    async def _guard(self, coro, fallback):
        try:
            out = await asyncio.wait_for(coro, timeout=self._t)
            self.healthy = True; return out
        except Exception as e:  # noqa: BLE001 — isolation is the point
            self.healthy = False; self.last_error = f"{type(e).__name__}: {e}"
            logger.warning("brain provider %s failed: %s", self.provider_id, self.last_error)
            return fallback

    async def remember(self, *a, **k): return await self._guard(self._inner.remember(*a, **k), None)
    async def recall(self, *a, **k) -> List[MemorySearchHit]: return await self._guard(self._inner.recall(*a, **k), [])
    async def list_memories(self, *a, **k): return await self._guard(self._inner.list_memories(*a, **k), [])
    async def delete(self, *a, **k): return await self._guard(self._inner.delete(*a, **k), False)
    async def search(self, *a, **k) -> List[BrainDoc]: return await self._guard(self._inner.search(*a, **k), [])
    async def categories(self, *a, **k) -> List[str]: return await self._guard(self._inner.categories(*a, **k), [])

class CombinedBrainProvider(MemoryProvider):
    """Fans search / categories / recall out to several brain providers and
    merges the results — powers the "All brains (combined)" option. Each result
    is tagged with its source brain (brain_id / brain_name). Read-only."""
    provider_id = "brain:all"
    display_name = "All brains (combined)"
    color = ""

    def __init__(self, providers):
        self._providers = list(providers)
        self.enabled = True
        self.writable = False
        self.healthy = True

    async def search(self, query: str, *, category=None, limit: int = 20) -> List[BrainDoc]:
        per = max(5, limit or 20)
        docs: List[BrainDoc] = []
        for p in self._providers:
            try:
                got = await p.search(query, category=category, limit=per)
            except Exception:
                got = []
            bid = getattr(p, "provider_id", ""); bname = getattr(p, "display_name", "")
            for d in got:
                try:
                    d.brain_id = bid; d.brain_name = bname
                except Exception:
                    pass
                docs.append(d)
        # Dedup by (title, category), keeping the highest score.
        best = {}
        for d in docs:
            k = (getattr(d, "title", ""), getattr(d, "category", ""))
            if k not in best or getattr(d, "score", 0) > getattr(best[k], "score", 0):
                best[k] = d
        items = list(best.values())
        if (query or "").strip():
            items.sort(key=lambda d: getattr(d, "score", 0), reverse=True)
            return items[:limit] if limit else items
        # Browse mode (empty query): group by category then title; return ALL
        # (don't cap browse — the single-brain path lists the whole vault too).
        items.sort(key=lambda d: ((getattr(d, "category", "") or "").lower(),
                                  (getattr(d, "title", "") or "").lower()))
        return items

    async def categories(self) -> List[str]:
        cats = set()
        for p in self._providers:
            try:
                for c in await p.categories():
                    cats.add(c)
            except Exception:
                pass
        return sorted(cats)

    async def recall(self, query: str, *, owner=None, top_k: int = 5) -> List[MemorySearchHit]:
        hits: List[MemorySearchHit] = []
        for p in self._providers:
            try:
                hits.extend(await p.recall(query, owner=owner, top_k=top_k))
            except Exception:
                pass
        hits.sort(key=lambda h: getattr(h, "score", 0), reverse=True)
        return hits[:top_k]

    async def remember(self, *a, **k):
        raise NotImplementedError("read-only combined brain")

    async def list_memories(self, *a, **k):
        return []

    async def delete(self, *a, **k):
        return False


def discover_providers(folder: str) -> List[MemoryProvider]:
    out: List[MemoryProvider] = []
    p = pathlib.Path(folder)
    if not p.is_dir(): return out
    for f in sorted(p.glob("*.py")):
        if f.name.startswith("_"): continue
        try:
            spec = importlib.util.spec_from_file_location(f"prisma_provider_{f.stem}", f)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)  # type: ignore
        except Exception as e:  # noqa: BLE001
            logger.error("failed loading provider %s: %s", f, e); continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, MemoryProvider) and obj is not MemoryProvider and obj.__module__ == mod.__name__:
                try: out.append(obj())
                except Exception as e:  # noqa: BLE001
                    logger.error("failed instantiating %s: %s", obj, e)
    return out
