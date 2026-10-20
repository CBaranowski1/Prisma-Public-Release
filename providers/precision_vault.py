"""Reference brain adapter: reads the Precision Audit vault (YAML-frontmatter
markdown under knowledge/<category>/*.md) with stdlib-only lexical recall.

Config: set the vault path via the PRISMA_BRAIN_VAULT env var or the Brain
settings UI. Defaults to empty (disabled) until configured.
"""
from __future__ import annotations
import os
import re
import pathlib
from typing import List, Optional
from src.memory_provider import (MemoryProvider, MemoryRecord, MemorySearchHit, BrainDoc)

_WORD = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")
_STOP = set("the a an and or of to in for on with is are was were be this that it as at by from".split())
_FM = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

def _terms(t: str) -> set:
    return {w for w in (m.group(0).lower() for m in _WORD.finditer(t or "")) if w not in _STOP}

class PrecisionVaultProvider(MemoryProvider):
    provider_id = "precision-audit"
    display_name = "Precision Audit Brain"
    writable = False

    def __init__(self, vault_path: Optional[str] = None,
                 provider_id: Optional[str] = None,
                 display_name: Optional[str] = None,
                 color: Optional[str] = None,
                 index_dir: Optional[str] = None):
        # _explicit_path: a path the caller supplied directly (not from env/settings).
        # When set, it takes priority over the saved settings value so unit tests
        # that pass vault_path= explicitly are unaffected by a saved settings value.
        self._explicit_path = vault_path or None
        self.vault_path = vault_path or os.getenv("PRISMA_BRAIN_VAULT", "")
        # Per-brain identity (modular brains): each brains/<id> folder gets its
        # own instance with a distinct provider_id/display_name/color. Falls back
        # to the class defaults when constructed bare (single-vault / tests).
        if provider_id:
            self.provider_id = provider_id
        if display_name:
            self.display_name = display_name
        self.color = (color or "")
        # Brain folder that holds an optional semantic index/ (built by
        # tools/index_brain.py). Lets recall() do fast vector search.
        self.index_dir = index_dir
        self._index = None

    def _vector_index(self):
        """The brain's semantic index if it's been built, else None (lazy)."""
        if self._index is None and self.index_dir:
            try:
                from src.brain_index import BrainIndex
                import pathlib
                self._index = BrainIndex(pathlib.Path(self.index_dir) / "index")
            except Exception:
                self._index = None
        return self._index

    @property
    def enabled(self) -> bool:
        # Reflect the EFFECTIVE path (explicit arg → saved setting → env), not just
        # the env value captured at construction. Otherwise a vault configured via
        # the Settings UI leaves the provider falsely disabled until a process that
        # happens to have PRISMA_BRAIN_VAULT set restarts it.
        return bool(self._effective_vault_path())

    def _effective_vault_path(self) -> str:
        """Return the vault path to use.

        Priority:
        1. An explicit vault_path passed to the constructor (unit-test / direct usage).
        2. The saved setting ``brain_vault_path`` (configured via Settings UI).
        3. The env-sourced ``self.vault_path`` (PRISMA_BRAIN_VAULT or empty).
        """
        if self._explicit_path:
            return self._explicit_path
        try:
            from src.settings import get_setting
            saved = get_setting("brain_vault_path", "") or ""
            if saved:
                return saved
        except Exception:
            pass
        return self.vault_path

    def _root(self) -> pathlib.Path:
        return pathlib.Path(self._effective_vault_path()) / "knowledge"

    def _rehydrate_body(self, note_id: str, category: str) -> Optional[str]:
        """Best-effort: read the full note body from the vault by id. Returns None
        (never raises) on any miss — caller falls back to the stored excerpt (D-03).

        Enforces vault-root containment before any read (ASVS V12): a corrupted or
        hand-edited meta.json id/category could otherwise carry a traversal segment
        (e.g. ``../../secrets``).
        """
        try:
            vault_root = self._root().resolve()
            path = (vault_root / category / f"{note_id}.md").resolve()
            # Descendant check, NOT a string prefix: a sibling directory like
            # ``knowledge_private`` shares the ``knowledge`` prefix and would slip
            # past a startswith() guard (e.g. category="../knowledge_private").
            # is_relative_to enforces a real path boundary. ValueError is caught
            # too because .resolve() raises it on a NUL-poisoned note_id/category
            # — the same corrupted-meta.json threat this guard defends (ASVS V12).
            if not path.is_relative_to(vault_root):
                return None
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        m = _FM.match(text)
        return text[m.end():] if m else text

    def _docs(self, cap: int = 8000):
        # Bounded: reading every file melts down at 100k+ notes (and blocks the event
        # loop). Consumers that need all-notes quality (search/recall) use the vector
        # index; this lexical walk is a bounded convenience/fallback.
        root = self._root()
        if not root.is_dir(): return
        for i, f in enumerate(root.glob("*/*.md")):
            if i >= cap: break
            text = f.read_text(encoding="utf-8", errors="replace")
            title = f.stem.replace("-", " ").title()
            m = _FM.match(text)
            if m:
                tm = re.search(r"title:\s*(.+)", m.group(1))
                if tm: title = tm.group(1).strip()
                text = text[m.end():]
            yield f, f.parent.name, title, text

    async def categories(self) -> List[str]:
        root = self._root()
        return sorted([d.name for d in root.iterdir() if d.is_dir()]) if root.is_dir() else []

    def _ranked(self, query, category):
        q = _terms(query)
        scored = []
        for f, cat, title, body in self._docs():
            if category and cat != category: continue
            overlap = len(q & _terms(title + " " + body))
            if overlap:
                # Keep lexical fallback scores on the same 0..1 scale as
                # semantic cosine/hybrid results.  Raw overlap counts let a
                # one-word virtual-profile match tie an exact document while
                # search() then discarded this score entirely (emitting 0.0).
                scored.append((overlap / len(q), f, cat, title, body))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored

    def _browse(self, category):
        """Every doc (optionally one category), sorted by category then title.
        Powers the default Brain browse view (empty query) so the tab lists the
        whole vault instead of forcing a search."""
        docs = [(f, cat, title, body) for f, cat, title, body in self._docs()
                if not category or cat == category]
        docs.sort(key=lambda x: (x[1].lower(), x[2].lower()))
        return docs

    async def search(self, query: str, *, category: Optional[str] = None, limit: int = 20) -> List[BrainDoc]:
        out = []
        if _terms(query):
            # Fast path: vector index (covers ALL notes in ms). Falls back to the
            # BOUNDED lexical ranker only if there is no index.
            idx = self._vector_index()
            if idx is not None and getattr(idx, "available", False):
                try:
                    hits = idx.search(query, top_k=limit)
                    if hits:
                        for h in hits:
                            if category and h.get("category", "") != category: continue
                            out.append(BrainDoc(id=h["id"], title=h.get("title", h["id"]),
                                                snippet=(h.get("excerpt", "") or "")[:240],
                                                category=h.get("category", ""), source_path=h["id"],
                                                score=float(h.get("score", 0.0))))
                        return out
                except Exception:
                    pass
            rows = self._ranked(query, category)[:limit]
        else:
            # No query terms → browse: return the whole (optionally filtered) vault,
            # grouped by category alphabetically. Unbounded so nothing is hidden.
            rows = self._browse(category)
        for row in rows:
            if _terms(query):
                score, f, cat, title, body = row
            else:
                f, cat, title, body = row
                score = 0.0
            snippet = " ".join(body.split())[:240]
            out.append(BrainDoc(id=str(f), title=title, snippet=snippet, category=cat,
                                source_path=str(f), score=float(score)))
        return out

    async def recall(self, query: str, *, owner=None, top_k: int = 5) -> List[MemorySearchHit]:
        # Fast path: semantic vector index (built by tools/index_brain.py). This
        # is what makes a big vault usable for recall — a millisecond cosine
        # top-k instead of a lexical scan of every note (which times out at scale).
        idx = self._vector_index()
        if idx is not None and idx.available:
            try:
                hits = []
                for h in idx.search(query, top_k=top_k):
                    body = (self._rehydrate_body(h["id"], h.get("category", ""))
                            or h.get("body") or h["excerpt"])
                    rec = MemoryRecord(id=h["id"], text=f"{h['title']}: {body}",
                                       category=h.get("category", ""), source=self.provider_id)
                    hits.append(MemorySearchHit(memory=rec, provider_id=self.provider_id, score=h["score"]))
                if hits:
                    return hits
            except Exception:
                pass  # fall through to lexical
        # Fallback: BOUNDED lexical overlap. A full scan reads every file and, being
        # synchronous, can't be cancelled by the async recall timeout - so at 100k+
        # notes it melts down (minutes, GPU idle). Cap the scan hard; the vector index
        # above is the real path for big vaults. Reindex if this cap is hit often.
        q = _terms(query)
        if not q:
            return []
        root = self._root()
        if not root.is_dir():
            return []
        LIMIT = 1200
        scored = []
        for i, f in enumerate(root.glob("*/*.md")):
            if i >= LIMIT:
                break
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            title = f.stem.replace("-", " ").title()
            m = _FM.match(body)
            if m:
                tm = re.search(r"title:\s*(.+)", m.group(1))
                if tm: title = tm.group(1).strip()
                body = body[m.end():]
            overlap = len(q & _terms(title + " " + body))
            if overlap:
                scored.append((overlap / len(q), f, f.parent.name, title, body))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, f, cat, title, body in scored[:top_k]:
            rec = MemoryRecord(id=str(f), text=f"{title}: {' '.join(body.split())}",
                               category=cat, source=self.provider_id)
            out.append(MemorySearchHit(memory=rec, provider_id=self.provider_id, score=float(score)))
        return out

    async def remember(self, *a, **k): raise NotImplementedError("read-only brain")
    async def list_memories(self, *a, **k): return []
    async def delete(self, *a, **k): return False
