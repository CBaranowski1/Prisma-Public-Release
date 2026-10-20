"""Per-brain semantic vector index.

Turns each knowledge note into an embedding (meaning -> vector) so recall over a
big vault is fast + semantic instead of a slow lexical scan of every file. The
index is a small local artifact under brains/<id>/index/:
    vectors.npy   float32 [N, dim]   (L2-normalized note embeddings)
    meta.json     {dim, meta:[{id,category,title,excerpt}, ...]}  (row-aligned)

Build it offline with tools/index_brain.py; PrecisionVaultProvider.recall loads
it lazily and does a cosine top-k (one matrix-vector product, milliseconds).
Backend-agnostic: uses src.embeddings.get_embedding_client() (HTTP embedder ->
local FastEmbed fallback). Falls back to lexical recall when no index exists.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Optional, Iterable, Tuple

from core.atomic_io import atomic_write_json, atomic_write_npy
from core.constants import MAX_LEARNED_ENTRIES_PER_BRAIN
from core.file_locks import get_lock

logger = logging.getLogger(__name__)

_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LEXICAL_WORD = re.compile(r"[a-z0-9]+")
_LEXICAL_STOP = frozenset(
    "a an and are as at be by for from in is it of on or that the this to was were with".split()
)


def _normalized_lexical_text(value: object) -> str:
    """Normalize titles, slugs, and prose for deterministic phrase matching."""
    return " ".join(_LEXICAL_WORD.findall(str(value or "").casefold()))


def _query_terms(value: object) -> tuple[str, tuple[str, ...]]:
    normalized = _normalized_lexical_text(value)
    return normalized, tuple(
        word for word in normalized.split() if word not in _LEXICAL_STOP
    )


def _lexical_fields(entry: Dict) -> tuple[str, str, str, str, str, str]:
    """Pre-normalize an index row once when the index partition is loaded."""
    title = _normalized_lexical_text(entry.get("title", ""))
    note_id = _normalized_lexical_text(entry.get("id", ""))
    category = _normalized_lexical_text(entry.get("category", ""))
    source_path = _normalized_lexical_text(
        entry.get("source_path") or f"knowledge/{category}/{note_id}.md"
    )
    excerpt = _normalized_lexical_text(entry.get("excerpt", ""))
    body = _normalized_lexical_text(entry.get("body", ""))
    return title, note_id, category, source_path, excerpt, body


def _lexical_relevance_prepared(
    query_text: str,
    terms: tuple[str, ...],
    fields: tuple[str, str, str, str, str, str],
) -> float:
    if not query_text or not terms:
        return 0.0

    title, note_id, category, source_path, excerpt, body = fields

    identity_fields = (title, note_id, source_path)
    if query_text in identity_fields:
        return 1.0
    if any(query_text in field for field in identity_fields if field):
        return 0.99

    # A one-word substring in a long excerpt is commonplace and must not be
    # promoted to an exact-phrase result.  Require a real phrase (2+ terms).
    if len(terms) >= 2 and any(
        query_text in field for field in (excerpt, body) if field
    ):
        return 0.98

    identity_haystack = " ".join(identity_fields + (category,))
    content_haystack = " ".join((excerpt, body))
    identity_coverage = sum(term in identity_haystack for term in terms) / len(terms)
    content_coverage = sum(term in content_haystack for term in terms) / len(terms)

    if len(terms) >= 2 and identity_coverage == 1.0:
        return 0.94
    if len(terms) >= 3 and content_coverage == 1.0:
        return 0.82
    coverage = max(identity_coverage, content_coverage)
    if len(terms) >= 3 and coverage >= 0.75:
        return 0.65
    return min(0.25, coverage * 0.25)


def _lexical_relevance(query: object, entry: Dict) -> float:
    """Return a conservative 0..1 lexical confidence for one index row.

    Exact identity and multi-word phrase matches are strong enough to rescue a
    note whose stored vector is stale.  Partial token overlap is deliberately
    weak so it can only act as a semantic tie-breaker, never replace a strong
    semantic result.  This is metadata-only: no vault file is opened on the hot
    search path.
    """
    query_text, terms = _query_terms(query)
    return _lexical_relevance_prepared(query_text, terms, _lexical_fields(entry))


def _hybrid_score(semantic_score: float, lexical_score: float) -> float:
    """Fuse scores without letting weak keyword overlap swamp semantics."""
    semantic = max(-1.0, min(1.0, float(semantic_score)))
    lexical = max(0.0, min(1.0, float(lexical_score)))
    if lexical >= 0.98:
        return 1.0
    if lexical >= 0.90:
        return max(semantic, 0.97)
    if lexical >= 0.80:
        return max(semantic, 0.92)
    # Partial overlap is a bounded 0.01 tie-break at most.
    return min(1.0, semantic + lexical * 0.04)


def _docs(vault: Path) -> Iterable[Tuple[str, str, str, str]]:
    """Yield (slug, category, title, body) for knowledge/<cat>/*.md — the same
    notes the graph + provider use."""
    root = Path(vault) / "knowledge"
    if not root.is_dir():
        return
    for f in sorted(root.glob("*/*.md")):
        if f.stem.startswith("_"):
            continue
        try:
            text = f.read_text("utf-8", "replace")
        except OSError:
            continue
        title = f.stem.replace("-", " ").replace("_", " ").title()
        body = text
        m = _FM.match(text)
        if m:
            tm = re.search(r"title:\s*(.+)", m.group(1))
            if tm:
                title = tm.group(1).strip()
            body = text[m.end():]
        yield f.stem, f.parent.name, title, body


def _excerpt(body: str, n: int = 400) -> str:
    return " ".join((body or "").split())[:n]


def _legacy_probe_text(vault: Path, entry: Dict) -> Optional[str]:
    """Reconstruct the exact text used to embed one indexed vault note."""
    note_path = (
        Path(vault)
        / "knowledge"
        / str(entry.get("category", ""))
        / f"{entry.get('id', '')}.md"
    )
    if not note_path.is_file():
        return None
    try:
        raw = note_path.read_text("utf-8", "replace")
    except OSError:
        return None
    match = _FM.match(raw)
    body = raw[match.end():] if match else raw
    return (str(entry.get("title", "")) + "\n" + body)[:2000]


def verify_and_stamp_legacy_index(
    index_dir: Path,
    *,
    vault: Optional[Path] = None,
    required_matches: int = 3,
    similarity_floor: float = 0.999,
) -> bool:
    """Safely add an embedder stamp to a legacy main index.

    Dimension equality alone is insufficient because different embedding
    models can emit vectors with the same width. This migration reconstructs
    exact build inputs from the vault, re-embeds a deterministic sample, and
    requires several near-identical stored vectors before writing identity
    metadata. A mismatched legacy index fails loudly rather than being
    mislabeled and queried in an incompatible vector space.

    Returns ``True`` when a stamp was written and ``False`` when the index was
    already stamped or cannot be verified because its paired vault is absent.
    """
    import numpy as np
    from src.embeddings import get_embedding_client, _embedder_identity

    directory = Path(index_dir)
    meta_path = directory / "meta.json"
    vectors_path = directory / "vectors.npy"
    vault_path = Path(vault) if vault is not None else directory.parent / "vault"
    if not (meta_path.is_file() and vectors_path.is_file() and vault_path.is_dir()):
        return False

    with get_lock(directory):
        document = json.loads(meta_path.read_text("utf-8"))
        if document.get("embedder") is not None:
            return False
        metadata = document.get("meta", [])
        vectors = np.load(vectors_path, mmap_mode="r")
        if vectors.ndim != 2 or len(vectors) != len(metadata) or not metadata:
            raise RuntimeError(
                f"legacy brain index {directory} cannot be verified: vector/meta shape mismatch"
            )

        # Spread probes across the index so a few edited or missing vault notes
        # do not prevent a safe migration.
        needed_matches = min(required_matches, len(metadata))
        candidate_count = min(
            len(metadata),
            max(needed_matches * 4, needed_matches),
        )
        positions = np.linspace(0, len(metadata) - 1, candidate_count, dtype=int)
        probe_positions = []
        probe_texts = []
        for position in dict.fromkeys(int(p) for p in positions):
            text = _legacy_probe_text(vault_path, metadata[position])
            if text is not None:
                probe_positions.append(position)
                probe_texts.append(text)
        if len(probe_texts) < needed_matches:
            return False

        embedder = get_embedding_client(backend="fastembed")
        if embedder is None:
            return False
        fresh = np.asarray(
            embedder.encode(probe_texts, normalize_embeddings=True),
            dtype=np.float32,
        )
        if fresh.ndim != 2 or fresh.shape[1] != vectors.shape[1]:
            raise RuntimeError(
                f"legacy brain index {directory} uses dim {vectors.shape[1]}, "
                f"but the live embedder uses dim {fresh.shape[1] if fresh.ndim == 2 else '?'}; "
                "reindex needed: python scripts/reindex_brain.py"
            )
        similarities = [
            float(np.dot(vectors[position], fresh[row]))
            for row, position in enumerate(probe_positions)
        ]
        matches = sum(score >= similarity_floor for score in similarities)
        if matches < needed_matches:
            raise RuntimeError(
                f"legacy brain index {directory} failed embedder verification "
                f"({matches}/{len(similarities)} matching probes); "
                "reindex needed: python scripts/reindex_brain.py"
            )

        document["embedder"] = _embedder_identity(embedder)
        atomic_write_json(str(meta_path), document)
        logger.info(
            "verified and stamped legacy brain index %s with %s (%d/%d probes)",
            directory,
            document["embedder"],
            matches,
            len(similarities),
        )
        return True


def build_index(vault: Path, out_dir: Path, *, batch: int = 256, progress=None) -> Tuple[int, int]:
    """Embed every note in `vault` and write the index to `out_dir`. Returns
    (note_count, dim). Raises if there is no embedding backend or no notes."""
    import numpy as np
    from src.embeddings import get_embedding_client, _embedder_identity

    emb = get_embedding_client(backend="fastembed")
    if emb is None:
        raise RuntimeError("no embedding backend available (start Ollama with an "
                           "embedding model, or ensure FastEmbed can download)")
    ids, meta, texts = [], [], []
    for slug, cat, title, body in _docs(vault):
        ids.append(slug)
        meta.append({"id": slug, "category": cat, "title": title, "excerpt": _excerpt(body)})
        texts.append((title + "\n" + body)[:2000])
    if not texts:
        raise RuntimeError("no notes found to index under %s/knowledge" % vault)

    vecs: List[List[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        vecs.extend(emb.encode(chunk, normalize_embeddings=True))
        if progress:
            progress(min(i + batch, len(texts)), len(texts))
    arr = np.asarray(vecs, dtype=np.float32)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_npy(str(out_dir / "vectors.npy"), arr)
    atomic_write_json(
        str(out_dir / "meta.json"),
        {"dim": int(arr.shape[1]), "embedder": _embedder_identity(emb), "meta": meta})
    return len(ids), int(arr.shape[1])


def _load_learned_doc(mp: Path) -> Dict:
    """Load learned.json, normalizing the legacy bare-list shape (no embedder
    identity -- what every learned.json on disk looks like before this fix)
    into {"dim": None, "embedder": None, "meta": [...]}, matching meta.json's
    {dim, embedder, meta} shape so `_check_embedder_identity`'s three-way
    compare (legacy-warn / match / mismatch-raise) applies identically to
    both partitions (WR-02). `dim` is carried through (30-06) so
    forget_learned/prune_learned can preserve the existing dim stamp when
    rewriting the pair instead of guessing it."""
    doc = json.loads(mp.read_text("utf-8"))
    if isinstance(doc, list):
        return {"dim": None, "embedder": None, "meta": doc}
    return {"dim": doc.get("dim"), "embedder": doc.get("embedder"), "meta": doc.get("meta", [])}


def _self_heal_learned_pair(index_dir: Path, arr, meta: List[Dict]):
    """Shared length self-heal for the learned partition (GRAY-BRN-07,
    reused by forget_learned/prune_learned): if `arr` (vector rows) and
    `meta` (note entries) disagree in length -- always "vectors ahead of
    meta" from a crash between append_learned's two paired writes, never the
    reverse -- truncate BOTH to the aligned prefix and WARN. Never raises.
    Mirrors BrainIndex._ensure_learned's identical inline logic."""
    if len(arr) != len(meta):
        n = min(len(arr), len(meta))
        logger.warning(
            "brain index %s: learned partition length mismatch "
            "(vectors=%d, meta=%d) -- truncating to aligned prefix "
            "(%d); likely a crash mid-write between learned.npy and "
            "learned.json", index_dir, len(arr), len(meta), n,
        )
        arr, meta = arr[:n], meta[:n]
    return arr, meta


def forget_learned(index_dir: Path, note_id: str) -> bool:
    """Remove ONE learned note (its paired vector row + meta entry) by id,
    keeping learned.npy/learned.json aligned (GRAY-BRN-06). Serialized via
    the same per-brain core.file_locks.get_lock used by append_learned.

    Self-heals any pre-existing length mismatch (same rule as
    BrainIndex._ensure_learned) BEFORE locating the id, so a forget can never
    operate on -- or produce -- a misaligned pair. Rewrites the aligned pair
    vector-first/meta-second (atomic_write_npy then atomic_write_json),
    matching append_learned's crash-safe ordering.

    Returns True if a matching note was found and removed, False otherwise
    (id not found, or learned.npy/.json missing entirely -- a safe no-op)."""
    import numpy as np
    d = Path(index_dir)
    vp, mp = d / "learned.npy", d / "learned.json"
    with get_lock(d):
        if not (vp.is_file() and mp.is_file()):
            return False
        arr = np.load(vp)
        doc = _load_learned_doc(mp)
        meta = doc["meta"]
        arr, meta = _self_heal_learned_pair(d, arr, meta)
        idx = next((i for i, m in enumerate(meta) if m.get("id") == note_id), None)
        if idx is None:
            return False
        arr = np.delete(arr, idx, axis=0)
        meta = meta[:idx] + meta[idx + 1:]
        # Vector file FIRST, meta file SECOND -- same crash-safe ordering as
        # append_learned: a crash here always leaves "vectors ahead of meta".
        atomic_write_npy(str(vp), arr)
        atomic_write_json(str(mp), {"dim": doc.get("dim"), "embedder": doc.get("embedder"), "meta": meta})
        return True


def prune_learned(index_dir: Path, keep: int = MAX_LEARNED_ENTRIES_PER_BRAIN) -> list:
    """Bounded, opt-in eviction for the learned partition (GRAY-BRN-06).

    Opt-in via the `brain_retention_enabled` setting (default OFF, same flag
    used by 30-05's memory retention) -- a strict no-op unless explicitly
    enabled, matching src/brain_learn.py::is_enabled's per-call get_setting
    read idiom (a live toggle takes effect on the next call, no restart).

    When enabled and the pair has more than `keep` rows, keeps the LAST
    `keep` rows -- learned notes are strictly append-ordered (no
    pinned/timestamp concept here, unlike memory.json), so "newest" means
    "last appended" -- drops the oldest, and rewrites an aligned pair
    (vector-first/meta-second, same ordering as append_learned/
    forget_learned). Returns the list of dropped note ids.

    Best-effort (src/tool_output_store.py::_prune_store precedent): any
    internal failure is logged and swallowed, never raised or propagated --
    prune must never break a write. Missing learned.npy/.json is a no-op."""
    try:
        from src.settings import get_setting
        if not get_setting("brain_retention_enabled", False):
            return []
    except Exception:
        return []
    import numpy as np
    d = Path(index_dir)
    vp, mp = d / "learned.npy", d / "learned.json"
    try:
        with get_lock(d):
            if not (vp.is_file() and mp.is_file()):
                return []
            arr = np.load(vp)
            doc = _load_learned_doc(mp)
            meta = doc["meta"]
            arr, meta = _self_heal_learned_pair(d, arr, meta)
            if keep < 0 or len(meta) <= keep:
                return []
            dropped = [m.get("id") for m in meta[:-keep]] if keep > 0 else [m.get("id") for m in meta]
            arr = arr[-keep:] if keep > 0 else arr[0:0]
            meta = meta[-keep:] if keep > 0 else []
            atomic_write_npy(str(vp), arr)
            atomic_write_json(str(mp), {"dim": doc.get("dim"), "embedder": doc.get("embedder"), "meta": meta})
            return dropped
    except Exception as e:
        logger.debug("brain index %s: prune_learned skipped: %s", d, e, exc_info=True)
        return []


def append_learned(index_dir: Path, note: Dict, embedder=None) -> None:
    """Append ONE learned note's embedding to index/learned.npy (+ learned.json).
    Kept separate from the big immutable vectors.npy so adding a learning is cheap
    (no rewriting a 100MB file). `note` = {id, category, title, excerpt, body?}.

    WR-02: learned.json now carries the same embedder-identity stamp as
    meta.json ({dim, embedder, meta}) so a mismatched/legacy embedder on the
    learned partition is detectable via the same three-way compare used for
    the main index, instead of silently combining mismatched-dim or
    mismatched-model vectors into `search`'s np.vstack.

    GRAY-BRN-07/08 (30-04): the whole load->mutate->write critical section is
    serialized per-brain via core.file_locks.get_lock(index_dir), and the two
    paired files are written atomically (temp-file + os.replace) in a FIXED
    order -- learned.npy FIRST, learned.json SECOND. A crash between the two
    always leaves "vectors ahead of meta" (never the reverse), which
    BrainIndex._ensure_learned self-heals by dropping the uncommitted extra
    vector row -- a committed note is never lost or misindexed."""
    import numpy as np
    from src.embeddings import get_embedding_client, _embedder_identity
    emb = embedder or get_embedding_client(backend="fastembed")
    if emb is None:
        raise RuntimeError("no embedding backend available")
    d = Path(index_dir)
    d.mkdir(parents=True, exist_ok=True)
    text = (note.get("title", "") + "\n" + (note.get("body") or note.get("excerpt", "")))[:2000]
    vec = np.asarray(emb.encode([text], normalize_embeddings=True)[0], dtype=np.float32)
    vp, mp = d / "learned.npy", d / "learned.json"
    with get_lock(d):
        if vp.is_file() and mp.is_file():
            arr = np.load(vp)
            meta = _load_learned_doc(mp)["meta"]
        else:
            arr = np.zeros((0, vec.shape[0]), dtype=np.float32)
            meta = []
        arr = np.vstack([arr, vec[None, :]])
        meta.append({"id": note["id"], "category": note.get("category", ""),
                     "title": note.get("title", note["id"]), "excerpt": note.get("excerpt", ""),
                     "body": note.get("body", "")})
        # Vector file FIRST: a crash here leaves the OLD aligned pair intact.
        atomic_write_npy(str(vp), arr)
        # Meta file SECOND (the "commit" record): a crash here leaves vectors
        # AHEAD of meta by exactly one row -- self-healed on load below.
        atomic_write_json(str(mp), {"dim": int(vec.shape[0]), "embedder": _embedder_identity(emb), "meta": meta})
        # GRAY-BRN-06 (30-06): opportunistic, best-effort cap -- a no-op
        # unless brain_retention_enabled is set. prune_learned already never
        # raises by contract; this bare try/except is defense-in-depth (same
        # shape 30-05 used for NativeMemoryProvider.remember's compact()
        # call) so a future contract change can never break this write. The
        # nested get_lock(d) inside prune_learned is safe: this section is
        # already inside `with get_lock(d):`, and get_lock hands out a
        # threading.RLock, so the same thread re-acquiring it never deadlocks.
        try:
            prune_learned(d)
        except Exception:
            pass


class BrainIndex:
    """Lazily-loaded vector index for one brain. `search` returns the top-k notes
    by cosine similarity (vectors are pre-normalized, so a dot product = cosine).
    Searches the immutable main index PLUS any appended `learned` vectors, and
    reloads the learned part when it changes (so new learnings show up at once)."""

    def __init__(self, index_dir: Path):
        self.dir = Path(index_dir)
        self._main_vecs = None
        self._main_meta: Optional[List[Dict]] = None
        self._main_lexical: list[tuple[str, str, str, str, str, str]] = []
        self._learned_vecs = None
        self._learned_meta: List[Dict] = []
        self._learned_lexical: list[tuple[str, str, str, str, str, str]] = []
        self._learned_mtime = -1.0

    @property
    def available(self) -> bool:
        if (self.dir / "vectors.npy").is_file() and (self.dir / "meta.json").is_file():
            return True
        # A brain with no main index can still be searchable if it only has learnings.
        return (self.dir / "learned.npy").is_file() and (self.dir / "learned.json").is_file()

    def _ensure_main(self):
        if self._main_vecs is None:
            import numpy as np
            vp, mp = self.dir / "vectors.npy", self.dir / "meta.json"
            if vp.is_file() and mp.is_file():
                self._main_vecs = np.load(vp)
                meta_doc = json.loads(mp.read_text("utf-8"))
                if meta_doc.get("embedder") is None:
                    if verify_and_stamp_legacy_index(self.dir):
                        meta_doc = json.loads(mp.read_text("utf-8"))
                self._main_meta = meta_doc.get("meta", [])
                self._main_lexical = [_lexical_fields(meta) for meta in self._main_meta]
                self._check_embedder_identity(meta_doc.get("embedder"), label="main index")
            else:
                self._main_vecs = np.zeros((0, 0), dtype="float32")
                self._main_meta = []
                self._main_lexical = []

    def _check_embedder_identity(self, stored_embedder, *, label: str = "main index"):
        """Three-way compare between the stored (on build) embedder identity and
        the live one, keyed on `stored_embedder`:
          - None (legacy meta.json/learned.json, no 'embedder' key -- the shape
            of this repo's REAL on-disk index today) -> WARN once and ACCEPT,
            never raise.
          - present and != live -> RAISE (fail loud, no auto-rebuild -- a silent
            re-embed of a large index is expensive and masks misconfiguration).
          - present and == live -> proceed silently.
        A None/unavailable live embedder degrades gracefully (no spurious raise).

        `label` identifies WHICH partition is being checked ("main index" or
        "learned partition", WR-02) so a mismatch/legacy warning is
        actionable about where to look and how to fix it."""
        from src.embeddings import get_embedding_client, _embedder_identity
        if stored_embedder is None:
            logger.warning(
                "brain index %s (%s): no embedder identity stamp (legacy) -- "
                "assuming current embedder is correct", self.dir, label,
            )
            return
        live_client = get_embedding_client(backend="fastembed")
        if live_client is None:
            return
        live = _embedder_identity(live_client)
        if stored_embedder != live:
            remedy = (
                "reindex needed: python scripts/reindex_brain.py" if label == "main index"
                else "delete index/learned.npy and index/learned.json to reset the learned "
                     "partition (learnings can be re-added)"
            )
            raise RuntimeError(
                f"embedder identity mismatch for {self.dir} ({label}): index built with "
                f"{stored_embedder}, live embedder is {live}. {remedy}"
            )

    def _ensure_learned(self):
        import numpy as np
        vp, mp = self.dir / "learned.npy", self.dir / "learned.json"
        if not (vp.is_file() and mp.is_file()):
            self._learned_vecs, self._learned_meta, self._learned_mtime = None, [], -1.0
            self._learned_lexical = []
            return
        mt = vp.stat().st_mtime
        if mt != self._learned_mtime:
            arr = np.load(vp)
            doc = _load_learned_doc(mp)
            self._check_embedder_identity(doc.get("embedder"), label="learned partition")
            meta = doc.get("meta", [])
            if len(arr) != len(meta):
                # GRAY-BRN-07 self-heal: a crash between append_learned's two
                # paired writes (learned.npy committed, learned.json not yet --
                # always this direction, never the reverse) leaves these
                # misaligned. Truncate BOTH to the aligned prefix and WARN
                # loudly -- never serve a misaligned vector<->note pair.
                # Mirrors _check_embedder_identity's warn-and-continue style
                # used elsewhere in this same method for legacy/degraded cases.
                n = min(len(arr), len(meta))
                logger.warning(
                    "brain index %s: learned partition length mismatch "
                    "(vectors=%d, meta=%d) -- truncating to aligned prefix "
                    "(%d); likely a crash mid-write between learned.npy and "
                    "learned.json", self.dir, len(arr), len(meta), n,
                )
                arr, meta = arr[:n], meta[:n]
            self._learned_vecs = arr
            self._learned_meta = meta
            self._learned_lexical = [_lexical_fields(item) for item in meta]
            self._learned_mtime = mt

    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        import numpy as np
        from src.embeddings import get_embedding_client
        emb = get_embedding_client(backend="fastembed")
        if emb is None or not query or top_k <= 0:
            return []
        self._ensure_main()
        self._ensure_learned()
        parts, metas, lexical_rows = [], [], []
        if self._main_vecs is not None and self._main_vecs.size:
            parts.append(self._main_vecs); metas.extend(self._main_meta)
            if len(self._main_lexical) != len(self._main_meta):
                self._main_lexical = [_lexical_fields(meta) for meta in self._main_meta]
            lexical_rows.extend(self._main_lexical)
        if self._learned_vecs is not None and self._learned_vecs.size:
            parts.append(self._learned_vecs); metas.extend(self._learned_meta)
            if len(self._learned_lexical) != len(self._learned_meta):
                self._learned_lexical = [_lexical_fields(meta) for meta in self._learned_meta]
            lexical_rows.extend(self._learned_lexical)
        if not parts:
            return []
        vecs = parts[0] if len(parts) == 1 else np.vstack(parts)
        q = np.asarray(emb.encode([query], normalize_embeddings=True)[0], dtype=np.float32)
        sims = vecs @ q
        k = min(top_k, len(metas))

        # Semantic retrieval supplies a bounded candidate pool.  In parallel,
        # scan the already-loaded compact metadata for only high-confidence
        # lexical matches.  This rescues exact titles/paths/phrases when an
        # embedding is stale without reading 100k+ vault files or allowing a
        # one-word match to dominate a genuinely strong vector hit.
        semantic_pool_size = min(len(metas), max(k * 4, 32))
        semantic_top = np.argpartition(-sims, semantic_pool_size - 1)[:semantic_pool_size]
        lexical_scores: Dict[int, float] = {}
        candidates = {int(i) for i in semantic_top}
        query_text, query_words = _query_terms(query)
        for i, fields in enumerate(lexical_rows):
            lexical = _lexical_relevance_prepared(query_text, query_words, fields)
            if lexical >= 0.65:
                candidates.add(i)
                lexical_scores[i] = lexical

        ranked = []
        for i in candidates:
            semantic = float(sims[i])
            lexical = lexical_scores.get(i)
            if lexical is None:
                lexical = _lexical_relevance_prepared(
                    query_text,
                    query_words,
                    lexical_rows[i],
                )
            ranked.append((_hybrid_score(semantic, lexical), lexical, semantic, i))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        out = []
        for hybrid, lexical, semantic, i in ranked[:k]:
            m = metas[i]
            out.append({"id": m["id"], "category": m.get("category", ""),
                        "title": m.get("title", m["id"]), "excerpt": m.get("excerpt", ""),
                        "body": m.get("body", ""),
                        "score": hybrid,
                        "semantic_score": semantic,
                        "lexical_score": lexical})
        return out
