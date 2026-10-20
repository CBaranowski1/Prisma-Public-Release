"""Tests for GRAY-BRN-02: Chroma collection dim + embedder identity
stamp/compare/namespaced-fallback, added to src/memory_vector.py,
src/rag_vector.py, and src/tool_index.py's `_stamp_or_check_collection_identity`
private helper (mirrored identically in all three modules).

Live ChromaDB is NOT importable in this environment (chromadb 1.5.8 is
installed but broken via a transitive opentelemetry import error;
chromadb-client, the actually-pinned package, is not installed at all) --
so this file drives the identity helper directly against a hand-rolled fake
Collection/Client double. No `import chromadb` at module scope (Pitfall 6).
"""
import json
import logging

import pytest

from src.memory_vector import _stamp_or_check_collection_identity as mv_identity
from src.rag_vector import _stamp_or_check_collection_identity as rag_identity
from src.tool_index import _stamp_or_check_collection_identity as ti_identity

# Parametrize over all three mirrored implementations to prove Task 1's
# "mirror the identical logic into rag_vector.py and tool_index.py"
# requirement, not just the primary (memory_vector.py) copy.
IDENTITY_FNS = [mv_identity, rag_identity, ti_identity]
IDENTITY_FN_IDS = ["memory_vector", "rag_vector", "tool_index"]


def _assert_scalar_metadata(metadata):
    """Mimic real ChromaDB's rejection of non-scalar collection metadata
    values (CR-01). Real ChromaDB requires metadata values to be
    str/int/float/bool -- a nested dict is rejected at validation time. The
    original fake doubles accepted arbitrary metadata, which let the
    nested-dict CR-01 bug pass unit tests while failing against the real
    dependency. Enforcing this here makes a regression to nested-dict storage
    fail loudly in this test file instead of silently passing.
    """
    if metadata is None:
        return
    for key, value in metadata.items():
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise TypeError(
                f"Chroma metadata value for key {key!r} must be str/int/float/bool "
                f"(real ChromaDB rejects non-scalar values), got {type(value).__name__}: {value!r}"
            )


class FakeCollection:
    """Hand-rolled fake Chroma Collection double.

    Exposes `.metadata` (settable) and `.modify(metadata=...)`, recording the
    last metadata passed to modify -- matching the confirmed chromadb 1.5.8
    API surface (`CollectionCommon.metadata` property, `Collection.modify()`).
    `.modify()` enforces scalar-only metadata values exactly as real ChromaDB
    does (CR-01).
    """

    def __init__(self, name, metadata=None):
        self.name = name
        self.metadata = metadata or {}
        self.modify_calls = []

    def modify(self, metadata=None):
        _assert_scalar_metadata(metadata)
        self.modify_calls.append(metadata)
        self.metadata = metadata


class RaisingCollection(FakeCollection):
    """A fake collection whose .modify() raises, to prove graceful degrade."""

    def modify(self, metadata=None):
        raise RuntimeError("simulated chroma modify failure")


class FakeClient:
    """Fake Chroma client: a name-keyed registry of FakeCollection objects.

    `get_or_create_collection` returns the existing fake collection for a
    given name, or creates+registers a new one -- mirroring the real client's
    get-or-create semantics closely enough for this unit's purposes.
    `get_or_create_collection` enforces scalar-only metadata values exactly
    as real ChromaDB does (CR-01).
    """

    def __init__(self):
        self.collections = {}
        self.create_calls = []

    def get_or_create_collection(self, name, metadata=None):
        _assert_scalar_metadata(metadata)
        self.create_calls.append((name, metadata))
        if name not in self.collections:
            self.collections[name] = FakeCollection(name, metadata)
        return self.collections[name]


class StubEmbeddingClient:
    """Stub embedding client exposing url/model so `_embedder_identity`
    (src/embeddings.py, Plan 11-03) produces a deterministic identity."""

    def __init__(self, dim=384, url="local://fastembed", model="sentence-transformers/all-MiniLM-L6-v2"):
        self._dim = dim
        self.url = url
        self.model = model

    def get_sentence_embedding_dimension(self):
        return self._dim


@pytest.mark.parametrize("identity_fn", IDENTITY_FNS, ids=IDENTITY_FN_IDS)
def test_legacy_collection_is_stamped_in_place(identity_fn):
    """A collection whose metadata lacks an 'embedder' key gets stamped via
    .modify() and the SAME collection object is returned (no fallback). The
    stamp is scalar-only (CR-01): a real ChromaDB rejects a nested-dict
    metadata value, and `FakeCollection.modify` now enforces the same
    constraint, so this test fails against the pre-fix nested-dict code and
    passes only once the embedder identity is serialized to a JSON string."""
    client = FakeClient()
    collection = FakeCollection("odysseus_memories", metadata={"hnsw:space": "cosine"})
    embedder = StubEmbeddingClient()

    result = identity_fn(collection, client, "odysseus_memories", embedder, logging.getLogger("test"))

    assert result is collection
    assert len(collection.modify_calls) == 1
    stamped = collection.modify_calls[0]
    assert stamped["dim"] == 384
    # Embedder identity must be a scalar (serialized JSON string), not a dict.
    assert isinstance(stamped["embedder"], str)
    embedder_obj = json.loads(stamped["embedder"])
    assert embedder_obj["backend"] == "fastembed"
    assert embedder_obj["model"] == "sentence-transformers/all-MiniLM-L6-v2"
    # hnsw:space must never be passed to .modify() (rejected by the real API)
    assert "hnsw:space" not in stamped
    # No fallback collection created
    assert client.create_calls == []

    # The stamp persists: re-running the check against the now-stamped
    # collection is a no-op (matching identity), proving the stamp is both
    # scalar-valid and comparable on a second pass.
    result2 = identity_fn(collection, client, "odysseus_memories", embedder, logging.getLogger("test"))
    assert result2 is collection
    assert len(collection.modify_calls) == 1  # no additional modify() call
    assert client.create_calls == []


@pytest.mark.parametrize("identity_fn", IDENTITY_FNS, ids=IDENTITY_FN_IDS)
def test_matching_identity_is_a_noop(identity_fn):
    """A collection whose stored {dim, embedder} already matches the live
    identity (stored in the same serialized-JSON-string scalar form a real
    stamp would use) triggers no .modify() call and no fallback collection."""
    client = FakeClient()
    embedder = StubEmbeddingClient()
    live_embedder_json = json.dumps(
        {"backend": "fastembed", "model": embedder.model, "url": embedder.url}, sort_keys=True
    )
    collection = FakeCollection(
        "odysseus_rag",
        metadata={"hnsw:space": "cosine", "dim": 384, "embedder": live_embedder_json},
    )

    result = identity_fn(collection, client, "odysseus_rag", embedder, logging.getLogger("test"))

    assert result is collection
    assert collection.modify_calls == []
    assert client.create_calls == []


@pytest.mark.parametrize("identity_fn", IDENTITY_FNS, ids=IDENTITY_FN_IDS)
def test_mismatched_identity_routes_to_namespaced_fallback(identity_fn, caplog):
    """A collection whose stored dim/embedder differs from the live identity
    must NOT be queried directly -- the wrapper builds a deterministic
    fallback collection name and uses that instead, logging an error. The
    fallback creation call's metadata must also be scalar-only (CR-01):
    `FakeClient.get_or_create_collection` enforces this, so this test fails
    against the pre-fix nested-dict code (the fallback creation would raise,
    get swallowed by the outer except, and the mismatched original collection
    would be returned instead)."""
    client = FakeClient()
    embedder = StubEmbeddingClient(dim=384, model="sentence-transformers/all-MiniLM-L6-v2")
    stale_embedder_json = json.dumps(
        {"backend": "http", "model": "some-other-model", "url": "http://elsewhere:11434"}, sort_keys=True
    )
    collection = FakeCollection(
        "odysseus_tool_index",
        metadata={"hnsw:space": "cosine", "dim": 768, "embedder": stale_embedder_json},
    )

    with caplog.at_level(logging.ERROR):
        result = identity_fn(collection, client, "odysseus_tool_index", embedder, logging.getLogger("test"))

    expected_name = "odysseus_tool_index__d384_fastembed"
    assert result is not collection
    assert result.name == expected_name
    assert client.create_calls[-1][0] == expected_name
    # The mismatched primary was never .modify()'d nor otherwise mutated
    assert collection.modify_calls == []
    assert any("identity mismatch" in r.message for r in caplog.records)


@pytest.mark.parametrize("identity_fn", IDENTITY_FNS, ids=IDENTITY_FN_IDS)
def test_dim_none_degrades_to_existing_collection_without_fallback(identity_fn):
    """WR-01: when the dimension probe fails (dim=None), the helper must
    treat this as 'cannot verify' and degrade to returning the original
    collection unchanged -- NEVER build a `__dNone_<backend>` fallback name
    or route real data to a junk collection."""
    client = FakeClient()
    collection = FakeCollection(
        "odysseus_memories",
        metadata={"hnsw:space": "cosine", "dim": 384, "embedder": "some-existing-stamp"},
    )

    class FailingDimEmbeddingClient:
        url = "local://fastembed"
        model = "sentence-transformers/all-MiniLM-L6-v2"

        def get_sentence_embedding_dimension(self):
            raise RuntimeError("dimension probe failed")

    embedder = FailingDimEmbeddingClient()

    result = identity_fn(collection, client, "odysseus_memories", embedder, logging.getLogger("test"))

    assert result is collection
    assert collection.modify_calls == []
    assert client.create_calls == []


def test_fake_collection_rejects_non_scalar_metadata():
    """The fake double itself enforces ChromaDB's scalar-only metadata
    constraint, so a regression back to nested-dict storage fails loudly
    here instead of silently passing (CR-01)."""
    collection = FakeCollection("x")
    with pytest.raises(TypeError):
        collection.modify(metadata={"embedder": {"backend": "fastembed"}})


def test_fake_client_rejects_non_scalar_metadata():
    """Same scalar-only enforcement on `get_or_create_collection` (used for
    the namespaced-fallback creation path)."""
    client = FakeClient()
    with pytest.raises(TypeError):
        client.get_or_create_collection("x", metadata={"embedder": {"backend": "fastembed"}})


@pytest.mark.parametrize("identity_fn", IDENTITY_FNS, ids=IDENTITY_FN_IDS)
def test_raising_modify_degrades_without_crashing(identity_fn):
    """A collection whose .modify() raises must degrade gracefully -- the
    caller is never crashed, and the original collection is returned."""
    client = FakeClient()
    collection = RaisingCollection("odysseus_memories", metadata={"hnsw:space": "cosine"})
    embedder = StubEmbeddingClient()

    # Must not raise.
    result = identity_fn(collection, client, "odysseus_memories", embedder, logging.getLogger("test"))

    assert result is collection
