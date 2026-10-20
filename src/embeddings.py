"""
embeddings.py

Embedding clients for RAG and memory vector search.

Priority order:
  1. HTTP API (Ollama / vLLM / llama.cpp) — set EMBEDDING_URL in .env
  2. Local fastembed (ONNX, ~50MB) — zero config fallback

Set EMBEDDING_URL in .env, e.g.:
  EMBEDDING_URL=http://localhost:11434/v1/embeddings   (ollama)
  EMBEDDING_URL=http://localhost:8000/v1/embeddings    (vllm / llama.cpp)
"""

import os

# Force HuggingFace/fastembed to COPY model files rather than symlink them on
# a network-backed cache dir (cross-OS; see core/hf_env_guard.py). Module-top
# position preserved: library entrypoints have no guaranteed prior dotenv
# load, and huggingface_hub reads this flag at import time, so it must be set
# before huggingface_hub is first imported. (app.py sets the same guard,
# post-dotenv, for the server entrypoint.)
from core.hf_env_guard import ensure_hf_symlinks_disabled_if_networked

ensure_hf_symlinks_disabled_if_networked()

import logging
from uuid import uuid4
import numpy as np
import httpx
from typing import List, Optional

from src.dgx_spark import load_dgx_spark_config
from src.tls_overrides import llm_verify

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "all-minilm:l6-v2"
_DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def _is_windows_platform() -> bool:
    """Isolate the platform check so tests never mutate the process-wide os.name."""
    return os.name == "nt"


class EmbeddingClient:
    """Drop-in replacement for SentenceTransformer.encode() using an HTTP API."""

    def __init__(self, url: Optional[str] = None, model: Optional[str] = None):
        configured_url = url or os.getenv("EMBEDDING_URL")
        configured_model = model or os.getenv("EMBEDDING_MODEL")
        headers: dict[str, str] = {}

        if not configured_url:
            try:
                dgx = load_dgx_spark_config()
            except ValueError as exc:
                logger.warning("DGX Spark embedding configuration ignored: %s", exc)
                dgx = None
            if dgx is not None and dgx.embedding_model:
                configured_url = dgx.resolved_embedding_url
                configured_model = configured_model or dgx.embedding_model
                headers = dgx.headers

        self.url = configured_url or (
            f"http://{os.getenv('LLM_HOST', 'localhost')}:11434/v1/embeddings"
        )
        self.model = configured_model or _DEFAULT_MODEL
        embedding_api_key = os.getenv("EMBEDDING_API_KEY", "").strip()
        if embedding_api_key:
            headers = {"Authorization": f"Bearer {embedding_api_key}"}
        self._headers = headers
        self._dim: Optional[int] = None
        # Short connect timeout so a DOWN embedding endpoint (e.g. Ollama not
        # running on :11434) fast-fails to the local FastEmbed fallback instead
        # of stalling startup ~30s per probe. Read stays generous for a real
        # endpoint (embedding a short string returns in well under a second).
        # connect stays short (a DOWN endpoint fast-fails to FastEmbed), but read is
        # generous: Ollama cold-loads the embed model on the first call (can take
        # 10-60s) and batch reindex requests are large. Override via EMBEDDING_READ_TIMEOUT.
        _read_to = float(os.getenv("EMBEDDING_READ_TIMEOUT", "180"))
        self._client = httpx.Client(
            timeout=httpx.Timeout(
                connect=3.0, read=_read_to, write=15.0, pool=3.0
            ),
            verify=llm_verify(),
        )

    def get_sentence_embedding_dimension(self) -> int:
        """Probe the endpoint for embedding dimension if not yet known."""
        if self._dim is not None:
            return self._dim
        # Embed a single word to discover the dimension
        vec = self.encode(["hello"])
        self._dim = vec.shape[1]
        logger.info(f"Embedding dimension: {self._dim} (model={self.model})")
        return self._dim

    def encode(
        self, texts: List[str], normalize_embeddings: bool = True
    ) -> np.ndarray:
        """Encode texts via the API. Returns (N, dim) float32 array."""
        if not texts:
            return np.array([], dtype="float32")

        # Batch in chunks of 64 to avoid oversized requests
        all_vecs = []
        for i in range(0, len(texts), 64):
            batch = texts[i : i + 64]
            resp = self._client.post(
                self.url,
                headers=self._headers,
                json={"input": batch, "model": self.model},
            )
            resp.raise_for_status()
            data = resp.json()

            # OpenAI format: {"data": [{"embedding": [...], "index": 0}, ...]}
            embeddings = data.get("data", [])
            embeddings.sort(key=lambda e: e.get("index", 0))
            for emb in embeddings:
                all_vecs.append(emb["embedding"])

        vecs = np.array(all_vecs, dtype="float32")

        if normalize_embeddings and vecs.size > 0:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            vecs = vecs / norms

        if self._dim is None and vecs.size > 0:
            self._dim = vecs.shape[1]

        return vecs


def _windows_selfheal_and_build(cache_dir: str, model_name: str, build_embedder):
    """Windows-only self-heal for a broken-symlink FastEmbed cache dir
    (GRAY-BRN-15 / threat T-32-rmtree).

    The HuggingFace-hub cache stores model files as symlinks
    (snapshots/<rev>/model.onnx -> ../../blobs/<hash>). On a network-share /
    UNC data dir Windows refuses to follow them ([WinError 1463]), and a
    cache copied between machines can carry dead symlinks too. Either way
    fastembed tries to load a broken symlink and fails *without*
    re-downloading, leaving semantic memory degraded.

    SAFE self-heal: on detecting a broken-symlink `models--*` root, the
    suspect dir is QUARANTINE-RENAMED (os.replace) aside -- never deleted
    up front. `build_embedder()` (the actual re-download trigger) is then
    called. Only on SUCCESS is the quarantine (the known-bad copy, now
    proven replaceable) deleted; on FAILURE the quarantine is restored to
    its original path and the exception re-raised, so the only known-good
    copy is never destroyed before a replacement is proven.

    The whole detect->quarantine->build->commit/restore critical section is
    guarded by `core.file_locks.get_lock` (Phase 30) so two threads in this
    process cannot race the same cache dir. That lock is process-local
    only -- it does NOT serialize a second OS process touching the same
    cache dir (see 32-RESEARCH.md Pitfall 4 / core/file_locks.py docstring).

    On non-Windows, or when no broken symlink is detected, this is a
    passthrough: `build_embedder()` is called directly with no lock/rename
    overhead, matching today's behavior exactly.
    """
    if not _is_windows_platform():
        return build_embedder()

    from pathlib import Path
    from core.file_locks import get_lock

    with get_lock(Path(cache_dir)):
        _root = None
        try:
            import glob
            for _onnx in glob.glob(os.path.join(cache_dir, "**", "*.onnx"), recursive=True):
                if os.path.islink(_onnx) and not os.path.exists(_onnx):
                    _candidate = _onnx
                    while os.path.basename(_candidate) and not os.path.basename(_candidate).startswith("models--"):
                        _parent = os.path.dirname(_candidate)
                        if _parent == _candidate:
                            break
                        _candidate = _parent
                    if os.path.basename(_candidate).startswith("models--"):
                        _root = _candidate
                        break
        except Exception as _e:
            logger.debug("embedding cache symlink-heal detection skipped: %s", _e)
            _root = None

        if _root is None:
            return build_embedder()

        quarantine = f"{_root}.quarantine.{uuid4().hex[:8]}"
        logger.warning(
            "Embedding cache has a broken symlink under %s; quarantining to %s "
            "so fastembed re-downloads real files (restored automatically if "
            "the re-download fails)", _root, quarantine,
        )
        os.replace(_root, quarantine)
        try:
            emb = build_embedder()
        except Exception:
            try:
                os.replace(quarantine, _root)
            except Exception as _restore_err:
                logger.error(
                    "Failed to restore quarantined embedding cache %s -> %s: %s "
                    "(manual recovery may be required)", quarantine, _root, _restore_err,
                )
            raise

        import shutil
        shutil.rmtree(quarantine, ignore_errors=True)
        return emb


class FastEmbedClient:
    """Local embedding client using fastembed (ONNX). No external service needed."""

    def __init__(self, model: Optional[str] = None):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise RuntimeError(
                "Local fastembed is not installed. Either install it "
                "(pip install fastembed) or point the app at a remote "
                "embeddings server."
            ) from e

        self.model = model or os.getenv("FASTEMBED_MODEL", _DEFAULT_FASTEMBED_MODEL)
        # Persistent cache under data/ so the model survives reboots and so
        # the download lands exactly where the admin panel's _is_downloaded()
        # check looks (both default to this same path).
        cache_dir = os.getenv("FASTEMBED_CACHE_PATH") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "fastembed_cache",
        )
        os.makedirs(cache_dir, exist_ok=True)
        # Windows self-heal: see _windows_selfheal_and_build's docstring for
        # the full rationale (broken HF-hub symlinks on network-share / UNC
        # data dirs, or a cache copied between machines). SAFE pattern:
        # quarantine-rename the suspect dir, prove a re-download succeeds,
        # THEN delete the quarantine (or restore it on failure) -- never an
        # unconditional rmtree of the only good copy (GRAY-BRN-15).
        self._embedding = _windows_selfheal_and_build(
            cache_dir,
            self.model,
            lambda: TextEmbedding(model_name=self.model, cache_dir=cache_dir),
        )
        self._dim: Optional[int] = None
        self.url = "local://fastembed"
        logger.info(f"FastEmbed loaded model={self.model}")

    def get_sentence_embedding_dimension(self) -> int:
        if self._dim is not None:
            return self._dim
        vec = self.encode(["hello"])
        self._dim = vec.shape[1]
        logger.info(f"Embedding dimension: {self._dim} (model={self.model})")
        return self._dim

    def encode(
        self, texts: List[str], normalize_embeddings: bool = True
    ) -> np.ndarray:
        """Encode texts locally. Returns (N, dim) float32 array."""
        if not texts:
            return np.array([], dtype="float32")

        vecs = np.array(list(self._embedding.embed(texts)), dtype="float32")

        if normalize_embeddings and vecs.size > 0:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1, norms)
            vecs = vecs / norms

        if self._dim is None and vecs.size > 0:
            self._dim = vecs.shape[1]

        return vecs


def _embedder_identity(emb) -> dict:
    """Best-effort identity descriptor for an embedding client -- never raises,
    missing/unavailable fields are None. Consumed by src/brain_index.py (and
    the Chroma collection wrappers) to stamp/compare which embedder produced a
    stored vector index, so a same-dim model swap can be detected instead of
    silently returning semantically-wrong recall.

    No revision/commit hash is included: fastembed's public API exposes no
    git-commit-style revision for its bundled models, so fabricating one would
    look authoritative without being meaningful.
    """
    if emb is None:
        return {"backend": None, "model": None, "url": None}
    url = getattr(emb, "url", None)
    backend = "fastembed" if url == "local://fastembed" else "http"
    return {"backend": backend, "model": getattr(emb, "model", None), "url": url}


def _load_persisted_endpoint() -> dict:
    """Load the custom embedding endpoint saved from the admin panel."""
    try:
        endpoint_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "embedding_endpoint.json",
        )
        if os.path.exists(endpoint_file):
            import json
            data = json.loads(open(endpoint_file, encoding="utf-8").read())
            if data.get("url"):
                return data
    except Exception:
        pass
    return {}


_http_embed_down = False  # process-level latch: skip re-probing a dead endpoint


def reset_http_embed_state():
    """Clear the 'HTTP embedding endpoint is down' latch so the next
    get_embedding_client() re-probes. Call this when the embedding endpoint
    setting changes (e.g. the user starts Ollama and saves the endpoint) —
    otherwise a latch tripped at startup would keep us on FastEmbed for the
    whole process even after the endpoint comes back."""
    global _http_embed_down
    _http_embed_down = False


def get_embedding_client(*, backend: str | None = None):
    """Factory: try HTTP API first, fall back to local fastembed.

    `backend="fastembed"` forces local FastEmbed unconditionally, bypassing the
    HTTP probe and the EMBEDDING_BACKEND/EMBEDDING_URL env vars. This is used
    ONLY by the brain index (src/brain_index.py) so its query embedder always
    matches a fastembed-built index, without affecting RAG / native memory /
    tool-selection (which keep honoring the env-configured backend via the
    default backend=None)."""
    global _http_embed_down

    # Explicit local pin: backend="fastembed" (scoped, brain-index only), or
    # EMBEDDING_BACKEND=fastembed / EMBEDDING_URL=local (env-configured) skips
    # the HTTP probe entirely - no 404 noise, and guarantees the query embedder
    # matches an index built with FastEmbed.
    if backend == "fastembed" \
            or os.getenv("EMBEDDING_BACKEND", "").strip().lower() == "fastembed" \
            or os.getenv("EMBEDDING_URL", "").strip().lower() in ("local", "none", "fastembed", "local://fastembed"):
        try:
            client = FastEmbedClient()
            client.get_sentence_embedding_dimension()
            logger.info(f"Using local FastEmbed (pinned): model={client.model}")
            return client
        except Exception as e:
            logger.error(f"FastEmbed init failed: {e}")
            return None

    # Check for a persisted custom endpoint (saved from admin panel)
    persisted = _load_persisted_endpoint()
    if persisted.get("url"):
        url = persisted["url"]
        model = persisted.get("model", "")
        # Also set in env so other code sees it
        os.environ["EMBEDDING_URL"] = url
        if model:
            os.environ["EMBEDDING_MODEL"] = model

    # Try the HTTP embedding API — unless we already found it down this process
    # (avoids paying the connect timeout again on every RAG/memory/tool probe).
    if not _http_embed_down:
        try:
            client = EmbeddingClient()
            client.get_sentence_embedding_dimension()  # health check
            logger.info(f"Using HTTP embedding API: {client.url} model={client.model}")
            return client
        except Exception as e:
            _http_embed_down = True
            logger.warning(f"HTTP embedding API unavailable ({e}); using local FastEmbed for the rest of this process")

    # Fall back to local fastembed
    try:
        client = FastEmbedClient()
        client.get_sentence_embedding_dimension()
        logger.info(f"Using local FastEmbed: model={client.model}")
        return client
    except ImportError:
        logger.error("fastembed not installed — run: pip install fastembed")
    except Exception as e:
        logger.error(f"FastEmbed init failed: {e}")

    return None
