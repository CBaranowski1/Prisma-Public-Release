"""Phase 31 integration-checker connection (SC-5).

Proves the phase's headline privacy + degradation behaviors on the paths the
app actually assembles -- not only on the literally-named helpers -- mirroring
tests/test_phase28_integration.py / tests/test_phase29_integration.py /
tests/test_phase30_integration.py's hermetic SC-5 discipline for this phase:

  - claim (a): a synthetic secret/PII-bearing (title, body) lesson passed
    through the REAL src.brain_learn.add_learning() -- with
    brain_learning_enabled explicitly True, so the opt-in flag is not what
    blocks it -- is WITHHELD from the shared brain: add_learning() returns
    False, no knowledge/lessons/*.md file is written, the real
    src.brain_index.append_learned is never called, and a WARNING is logged
    (GRAY-BRN-03). A clean control lesson through the SAME path DOES write +
    index, proving the gate is selective (secret/PII only), not a blanket
    off-switch.

  - claim (b): a NativeMemoryProvider over a real MemoryManager(tmp_path),
    backed by a FakeVectorStore that is unhealthy (embedder/vector-store
    absent), drives recall() end-to-end: every returned hit is tagged
    mode='lexical', low_confidence=True, exactly one WARNING is logged for
    the process (the _lexical_fallback_warned latch is reset first so
    cross-test state cannot mask a regression), and identity-heavy injection
    stays bounded at IDENTITY_MEMORY_INJECTION_CAP even though far more
    identity memories were stored and a large top_k was requested
    (GRAY-BRN-05 / GRAY-BRN-13).

Hermetic: no live fastembed, Chroma, DB, LLM, or network is touched -- this
file must NOT be gated behind pytest.importorskip('fastembed'). Claim (a)
stubs src.brain_index.append_learned with a call-recording fake (mirrors
tests/test_brain_learn.py's _brain(tmp_path) helper). Claim (b) uses a
module-local FakeVectorStore (mirrors tests/test_memory_provider.py /
tests/test_recall_degradation.py). Assertions never touch the raw secret
literal -- only the return value, absence of a file, the stub's
not-called state, and the WARNING's reason class.

OUT OF SC-5 SCOPE (documented, not tested-as-fixed here): the live
real-server/real-store confirmation (a real chat pasting a secret and
checking server.log; a real embedder-absent run) is this plan's Task 3
human smoke, not this hermetic suite.
"""
import asyncio
import logging

import src.brain_index as bi
import src.brain_learn as bl


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Claim (a) -- secret/PII-bearing lesson withheld from the shared brain
# (GRAY-BRN-03).
# ---------------------------------------------------------------------------

def _brain(tmp_path):
    (tmp_path / "knowledge" / "code").mkdir(parents=True)
    return {"id": "t", "path": str(tmp_path), "dir": str(tmp_path)}


def test_secret_bearing_lesson_withheld_from_shared_brain_sc5(tmp_path, monkeypatch, caplog):
    appended = {}

    def fake_append(index_dir, note, embedder=None):
        appended["called"] = True

    monkeypatch.setattr(bi, "append_learned", fake_append)

    # Explicitly enable shared auto-learning so the PII/secret gate -- not
    # the private-by-default opt-in flag -- is what blocks this write.
    import src.settings as settings
    monkeypatch.setattr(
        settings, "get_setting",
        lambda k, d=None: True if k == "brain_learning_enabled" else d,
    )
    assert bl.is_enabled() is True

    brain = _brain(tmp_path)
    # Synthetic, obviously-fake OpenAI-shaped key -- never a real credential.
    secret_body = (
        "Our deployment script uses the key "
        "sk-FAKE1234567890ABCDEFGH1234 for testing purposes only."
    )

    with caplog.at_level(logging.WARNING):
        ok = bl.add_learning(brain, "Deployment key note", secret_body)

    assert ok is False, "secret-bearing lesson must be withheld, not written"
    assert not (tmp_path / "knowledge" / "lessons").exists(), (
        "no lessons directory (and therefore no file) may be created for a "
        "withheld lesson"
    )
    assert "called" not in appended, "append_learned must never be called for a withheld lesson"
    assert any("withheld" in r.message.lower() for r in caplog.records), (
        "a withheld lesson must emit a WARNING naming the withhold"
    )

    # Control: a clean lesson through the SAME real add_learning() path DOES
    # write + index -- proves the gate is selective (secret/PII only), not a
    # blanket learning off-switch.
    clean_ok = bl.add_learning(
        brain, "Atomic writes rule",
        "Always write settings JSON atomically to avoid corruption on crash during shutdown.",
    )
    assert clean_ok is True
    lessons = list((tmp_path / "knowledge" / "lessons").glob("*.md"))
    assert len(lessons) == 1, "the clean control lesson must be written to disk"
    assert appended.get("called") is True, "the clean control lesson must reach append_learned"


# ---------------------------------------------------------------------------
# Claim (b) -- degraded (embedder-absent) native recall is tagged lexical,
# warned once, and bounded (GRAY-BRN-05 / GRAY-BRN-13).
# ---------------------------------------------------------------------------

class FakeVectorStore:
    """Mirrors tests/test_memory_provider.py's / tests/test_recall_degradation.py's
    FakeVectorStore -- an unhealthy (embedder/vector-store absent) fake so
    NativeMemoryProvider.recall() is driven end-to-end without fastembed/Chroma."""

    def __init__(self, healthy=False):
        self.healthy = healthy
        self.added = []
        self.removed = []
        self.results = []

    def add(self, memory_id, text):
        self.added.append((memory_id, text))

    def remove(self, memory_id):
        self.removed.append(memory_id)

    def search(self, query, k=5):
        return self.results[:k]


def test_degraded_recall_tagged_lexical_warned_once_and_bounded_sc5(tmp_path, monkeypatch, caplog):
    from core.constants import IDENTITY_MEMORY_INJECTION_CAP
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider
    import src.memory_provider as memory_provider

    # Reset the module-level WARN-once latch so cross-test/process state
    # cannot mask a regression in this assertion.
    monkeypatch.setattr(memory_provider, "_lexical_fallback_warned", False)

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore(healthy=False)  # embedder/vector-store absent
    provider = NativeMemoryProvider(manager, vector)

    # Seed more identity memories than the cap, so an unbounded implementation
    # would flood the context -- this drives the bound end-to-end through the
    # REAL recall() path, not get_relevant_memories() in isolation.
    n_identity = IDENTITY_MEMORY_INJECTION_CAP + 2
    for i in range(n_identity):
        run(provider.remember(f"My name is Person{i}", owner="alice"))

    with caplog.at_level(logging.WARNING, logger="src.memory_provider"):
        hits = run(provider.recall("who am i", owner="alice", top_k=20))
        # A second recall in the same process must add no further warning.
        run(provider.recall("who am i", owner="alice", top_k=20))

    assert hits, "degraded recall must still return hits via the lexical fallback"
    assert all(h.mode == "lexical" for h in hits), "every hit must be tagged lexical when degraded"
    assert all(h.low_confidence is True for h in hits), "every degraded hit must be low_confidence"
    assert len(hits) <= IDENTITY_MEMORY_INJECTION_CAP, (
        f"identity injection must stay bounded at {IDENTITY_MEMORY_INJECTION_CAP}, "
        f"got {len(hits)} from {n_identity} stored identity memories"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        "exactly one WARNING must fire for the process across both recall() calls"
    )
