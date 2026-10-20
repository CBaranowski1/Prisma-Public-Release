"""
memory_vector.py

ChromaDB-backed vector store for memory entries.
Shares the EmbeddingClient with RAG to save memory.
Stores pre-computed embeddings (ChromaDB does not manage embedding).
"""

import json
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def _stamp_or_check_collection_identity(collection, client, collection_name, embedding_client, log=None):
    """Stamp or compare a Chroma collection's dim + embedder identity (GRAY-BRN-02).

    `get_or_create_collection(..., metadata=...)`'s own `metadata` kwarg is
    SILENTLY IGNORED whenever the named collection already exists (confirmed
    from the installed chromadb client's own docstring) -- so delivering an
    identity stamp to a pre-existing collection requires this explicit
    `.metadata` read + `.modify()` step; reuses `_embedder_identity` from
    src/embeddings.py (Plan 11-03) to build the live identity stamp.

    ChromaDB metadata values must be scalar (str/int/float/bool) -- a nested
    dict value is rejected at validation time. `_embedder_identity(...)`
    returns a dict, so it is serialized to a single deterministic JSON string
    (`json.dumps(..., sort_keys=True)`) before ever being stored or compared
    (CR-01 fix); comparisons happen in that same serialized string form.

    Behavior:
      - dimension probe failed (dim is None): cannot verify identity --
        degrades to returning the original `collection` unchanged. Never
        builds a fallback name containing the literal string "None" (WR-01).
      - legacy collection (no 'embedder' key in stored metadata): stamps
        {dim, embedder} in place via `.modify()`, keeps using the SAME collection.
      - matching identity: no-op, same collection.
      - mismatched dim/embedder: builds a deterministic namespaced fallback
        collection (f"{collection_name}__d{dim}_{backend}"), logs an error,
        and returns the fallback -- the mismatched primary is never queried.

    Never raises -- any failure degrades to returning the original
    `collection` unchanged, mirroring this module's existing self._healthy
    degrade convention so an identity-check failure never crashes the caller.
    Mirrored identically in src/rag_vector.py and src/tool_index.py.
    """
    log = log or logger
    try:
        from src.embeddings import _embedder_identity

        try:
            dim = embedding_client.get_sentence_embedding_dimension()
        except Exception:
            dim = None

        if dim is None:
            # Cannot verify identity without a dimension -- degrade to the
            # existing collection rather than treating None as a mismatch
            # and routing real data to a junk `__dNone_<backend>` collection.
            log.warning(
                "Chroma identity check for %s: dimension probe failed -- "
                "degrading to existing collection without verification",
                collection_name,
            )
            return collection

        embedder_identity = _embedder_identity(embedding_client)
        embedder_json = json.dumps(embedder_identity, sort_keys=True)
        live_identity = {"dim": dim, "embedder": embedder_json}
        stored = collection.metadata or {}

        if "embedder" not in stored:
            new_meta = {k: v for k, v in stored.items() if k != "hnsw:space"}
            new_meta.update(live_identity)
            collection.modify(metadata=new_meta)
            return collection

        if stored.get("dim") != live_identity["dim"] or stored.get("embedder") != live_identity["embedder"]:
            backend = embedder_identity.get("backend")
            fallback_name = f"{collection_name}__d{live_identity['dim']}_{backend}"
            fallback = client.get_or_create_collection(
                name=fallback_name,
                metadata={"hnsw:space": "cosine", **live_identity},
            )
            log.error(
                "Chroma collection %s identity mismatch (stored=%s, live=%s) -- "
                "querying namespaced fallback %s instead",
                collection_name, stored, live_identity, fallback_name,
            )
            return fallback

        return collection
    except Exception as e:
        log.error("Chroma identity check failed for %s: %s -- using existing collection", collection_name, e)
        return collection


class MemoryVectorStore:
    """Vector index over memory entries for semantic retrieval."""

    COLLECTION_NAME = "odysseus_memories"

    def __init__(self, data_dir: str, embedding_model=None):
        self._model = embedding_model
        self._collection = None
        self._healthy = False

        self._initialize()

    def _initialize(self):
        try:
            from src.chroma_client import get_chroma_client

            if self._model is None:
                from src.embeddings import get_embedding_client
                self._model = get_embedding_client()
                if self._model is None:
                    raise RuntimeError("No embedding backend available")
                logger.info(f"MemoryVectorStore using embeddings: {self._model.url}")

            client = get_chroma_client()
            self._collection = client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            self._collection = _stamp_or_check_collection_identity(
                self._collection, client, self.COLLECTION_NAME, self._model, logger,
            )

            self._healthy = True
            count = self._collection.count()
            logger.info(f"MemoryVectorStore ready (entries={count})")

        except Exception as e:
            logger.error(f"MemoryVectorStore init failed: {e}")

    @property
    def healthy(self) -> bool:
        return self._healthy

    def _embed(self, texts: List[str]) -> List[List[float]]:
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return vecs.tolist()

    def count(self) -> int:
        """Return the number of stored vectors."""
        if not self._healthy:
            return 0
        return self._collection.count()

    def add(self, memory_id: str, text: str):
        """Add a single memory entry to the vector index."""
        if not self._healthy:
            return
        # Skip if already exists
        existing = self._collection.get(ids=[memory_id])
        if existing["ids"]:
            return
        embeddings = self._embed([text])
        self._collection.add(
            ids=[memory_id],
            embeddings=embeddings,
            documents=[text],
            metadatas=[{"source": "memory"}],
        )

    def remove(self, memory_id: str):
        """Remove a memory entry. O(1) — no rebuild needed."""
        if not self._healthy:
            return
        try:
            self._collection.delete(ids=[memory_id])
        except Exception as e:
            logger.warning(f"memory remove {memory_id}: {e}")

    def search(self, query: str, k: int = 8) -> List[Dict]:
        """Search for the most relevant memory IDs by semantic similarity.
        Returns list of {"memory_id": str, "score": float}.

        ChromaDB cosine distance = 1 - cosine_similarity.
        We convert back: similarity = 1.0 - distance.
        """
        if not self._healthy or self._collection.count() == 0:
            return []

        embeddings = self._embed([query])
        actual_k = min(k, self._collection.count())
        results = self._collection.query(
            query_embeddings=embeddings,
            n_results=actual_k,
        )

        out = []
        for idx, mid in enumerate(results["ids"][0]):
            distance = results["distances"][0][idx]
            out.append({
                "memory_id": mid,
                "score": round(1.0 - distance, 4),
            })
        return out

    def find_similar(self, text: str, threshold: float = 0.92) -> Optional[str]:
        """Check if a near-duplicate exists. Returns memory_id if found, else None."""
        if not self._healthy or self._collection.count() == 0:
            return None

        embeddings = self._embed([text])
        results = self._collection.query(
            query_embeddings=embeddings,
            n_results=1,
        )

        if results["ids"][0]:
            distance = results["distances"][0][0]
            similarity = 1.0 - distance
            if similarity >= threshold:
                return results["ids"][0][0]
        return None

    def rebuild(self, memories: List[Dict]):
        """Rebuild the entire index from a list of memory entries.
        Each entry must have 'id' and 'text' keys."""
        if not self._healthy:
            return

        from src.chroma_client import get_chroma_client

        # Delete and recreate collection for a clean rebuild
        client = get_chroma_client()
        try:
            client.delete_collection(self.COLLECTION_NAME)
        except Exception:
            pass
        self._collection = client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

        texts = []
        ids = []
        for mem in memories:
            text = mem.get("text", "").strip()
            mid = mem.get("id", "")
            if text and mid:
                texts.append(text)
                ids.append(mid)

        if texts:
            # Batch in chunks of 100 to avoid oversized requests
            for i in range(0, len(texts), 100):
                batch_texts = texts[i:i + 100]
                batch_ids = ids[i:i + 100]
                embeddings = self._embed(batch_texts)
                self._collection.add(
                    ids=batch_ids,
                    embeddings=embeddings,
                    documents=batch_texts,
                    metadatas=[{"source": "memory"}] * len(batch_ids),
                )

        logger.info(f"MemoryVectorStore rebuilt with {len(ids)} entries")
