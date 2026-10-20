"""Tests for GRAY-BRN-13 (bounded degraded native recall) and GRAY-BRN-05
(recall-mode tagging + WARN-once + mid-process failure catch).

Hermetic: every test drives get_relevant_memories()/NativeMemoryProvider.recall()
with a None or fake vector store -- no fastembed, no real ChromaDB. This file
must NOT be gated behind pytest.importorskip('fastembed').

Sections:
- threshold: LEXICAL_FALLBACK_SIMILARITY_THRESHOLD + IDENTITY_MEMORY_INJECTION_CAP
  behavior in src/memory.py::get_relevant_memories (run via `-k threshold`).
- tagging/WARN-once/mid-process-failure: src/memory_provider.py::
  NativeMemoryProvider.recall()'s mode/low_confidence tagging, the
  _lexical_fallback_warned latch, and the try/except around a mid-process
  vector-search failure.
"""

import asyncio
import logging


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Threshold + identity-injection-cap section (GRAY-BRN-13)
# ---------------------------------------------------------------------------


def test_threshold_drops_weak_submatch(tmp_path):
    """A low-overlap memory that scored just above the old 0.05 threshold but
    below the raised LEXICAL_FALLBACK_SIMILARITY_THRESHOLD (0.2) is excluded."""
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))

    # query tokens: {explain, machine, learning, basics}
    # memory tokens: {learning, is, fun}
    # intersection={learning}=1, union=6 -> jaccard ~= 0.1667 (no boost applies
    # since query_type classifies as "fact", which gets no score boost).
    memories = [{"id": "weak-1", "text": "learning is fun", "timestamp": 1}]

    result = manager.get_relevant_memories(
        "explain machine learning basics", memories, max_items=8
    )

    assert result == []


def test_threshold_keeps_strong_match(tmp_path):
    """A genuinely relevant memory (exact-phrase match, boosted to >=0.8)
    still clears the raised threshold."""
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))

    memories = [
        {
            "id": "strong-1",
            "text": "Reminder: urgent project deadline is Friday",
            "timestamp": 1,
        }
    ]

    result = manager.get_relevant_memories(
        "urgent project deadline", memories, max_items=8
    )

    assert [m["id"] for m in result] == ["strong-1"]


def test_threshold_preserves_markdown_preference_fallback_regression(tmp_path):
    """Non-regression guard (Pitfall/prohibition): the existing
    tests/test_memory_provider.py markdown-preference fallback case must
    still return its hit after the threshold raise (it scores ~0.26)."""
    from src.memory import MemoryManager

    manager = MemoryManager(str(tmp_path))

    memories = [
        {"id": "md-1", "text": "Alice prefers markdown notes", "timestamp": 1}
    ]

    result = manager.get_relevant_memories(
        "markdown preference", memories, max_items=3
    )

    assert [m["id"] for m in result] == ["md-1"]


def test_threshold_bounds_identity_injection_cap(tmp_path):
    """For an identity-classified query with more than
    IDENTITY_MEMORY_INJECTION_CAP identity memories present, at most the cap
    is injected -- the most-recently-added ones."""
    from src.memory import MemoryManager
    from core.constants import IDENTITY_MEMORY_INJECTION_CAP

    manager = MemoryManager(str(tmp_path))

    identity_memories = [
        {"id": f"identity-{i}", "text": f"My name is Person{i}", "timestamp": i}
        for i in range(IDENTITY_MEMORY_INJECTION_CAP + 2)
    ]

    result = manager.get_relevant_memories(
        "who am i", identity_memories, max_items=20
    )

    assert len(result) == IDENTITY_MEMORY_INJECTION_CAP
    expected_ids = [m["id"] for m in identity_memories[-IDENTITY_MEMORY_INJECTION_CAP:]]
    assert [m["id"] for m in result] == expected_ids


# ---------------------------------------------------------------------------
# Tagging / WARN-once / mid-process-failure section (GRAY-BRN-05)
# ---------------------------------------------------------------------------


class FakeVectorStore:
    """Mirrors tests/test_memory_provider.py's FakeVectorStore, extended with
    an optional raise_on_search to simulate a mid-process embedder failure
    (Pitfall 4: MemoryVectorStore.healthy is init-time-only)."""

    healthy = True

    def __init__(self, results=None, raise_on_search=False):
        self.results = results or []
        self.raise_on_search = raise_on_search
        self.search_calls = 0
        self.added = []
        self.removed = []

    def add(self, memory_id, text):
        self.added.append((memory_id, text))

    def remove(self, memory_id):
        self.removed.append(memory_id)

    def search(self, query, k=5):
        self.search_calls += 1
        if self.raise_on_search:
            raise RuntimeError("embedder mid-process failure")
        return self.results[:k]


def _reset_warn_latch(monkeypatch):
    import src.memory_provider as memory_provider

    monkeypatch.setattr(memory_provider, "_lexical_fallback_warned", False)


def test_tagging_no_vector_store_fallback_is_lexical_low_confidence(tmp_path, monkeypatch):
    _reset_warn_latch(monkeypatch)
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    provider = NativeMemoryProvider(manager)  # no vector store at all
    run(provider.remember("Alice prefers markdown notes", owner="alice", category="preference"))

    hits = run(provider.recall("markdown preference", owner="alice", top_k=3))

    assert len(hits) == 1
    assert hits[0].mode == "lexical"
    assert hits[0].low_confidence is True


def test_tagging_warn_once_fires_exactly_once_per_process(tmp_path, monkeypatch, caplog):
    _reset_warn_latch(monkeypatch)
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    provider = NativeMemoryProvider(manager)  # no vector store -> always degraded
    run(provider.remember("Alice prefers markdown notes", owner="alice", category="preference"))

    with caplog.at_level(logging.WARNING, logger="src.memory_provider"):
        run(provider.recall("markdown preference", owner="alice", top_k=3))
        run(provider.recall("markdown preference", owner="alice", top_k=3))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_tagging_mid_process_vector_failure_falls_back_to_lexical(tmp_path, monkeypatch):
    _reset_warn_latch(monkeypatch)
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore(raise_on_search=True)
    provider = NativeMemoryProvider(manager, vector)
    run(provider.remember("Alice prefers markdown notes", owner="alice", category="preference"))

    hits = run(provider.recall("markdown preference", owner="alice", top_k=3))

    assert len(hits) == 1
    assert hits[0].mode == "lexical"
    assert hits[0].low_confidence is True
    assert vector.search_calls == 1


def test_tagging_mid_process_vector_failure_fires_warn_once(tmp_path, monkeypatch, caplog):
    _reset_warn_latch(monkeypatch)
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore(raise_on_search=True)
    provider = NativeMemoryProvider(manager, vector)
    run(provider.remember("Alice prefers markdown notes", owner="alice", category="preference"))

    with caplog.at_level(logging.WARNING, logger="src.memory_provider"):
        run(provider.recall("markdown preference", owner="alice", top_k=3))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_tagging_healthy_empty_vector_result_falls_back_without_warning(tmp_path, monkeypatch, caplog):
    _reset_warn_latch(monkeypatch)
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore(results=[])  # healthy, genuinely zero matches
    provider = NativeMemoryProvider(manager, vector)
    run(provider.remember("Alice prefers markdown notes", owner="alice", category="preference"))

    with caplog.at_level(logging.WARNING, logger="src.memory_provider"):
        hits = run(provider.recall("markdown preference", owner="alice", top_k=3))

    # Still falls through to the lexical branch (no vector hits), but this is
    # NOT a genuine degradation -- empty != degraded, so no WARN fires.
    assert len(hits) == 1
    assert hits[0].mode == "lexical"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0


def test_tagging_healthy_vector_hits_stay_mode_vector_no_warning(tmp_path, monkeypatch, caplog):
    _reset_warn_latch(monkeypatch)
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore()
    provider = NativeMemoryProvider(manager, vector)
    saved = run(provider.remember("Alice likes green tea", owner="alice"))
    vector.results = [{"memory_id": saved.id, "score": 0.9}]

    with caplog.at_level(logging.WARNING, logger="src.memory_provider"):
        hits = run(provider.recall("what does alice like", owner="alice", top_k=3))

    assert len(hits) == 1
    assert hits[0].mode == "vector"
    assert hits[0].low_confidence is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 0
