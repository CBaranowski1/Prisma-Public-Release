"""Phase-37 automated re-encoding of Phase 31's deferred live smoke.

Phase 31's 31-04 Task 3 (see
``.planning/phases/31-recall-policy-privacy-degradation/31-VERIFICATION.md``)
deferred to a HUMAN live smoke: paste a secret into a real chat and confirm
server.log shows it withheld, then run with the embedder absent and confirm
degraded recall is tagged/bounded/warned-once. Both properties are pure
in-process logic with no LLM/network/UI dependency, so this file proves them
FULLY automated instead -- closing the deferred-smoke IOU with no residual.

Hermetic: no fastembed/Chroma/DB/LLM/network is touched here; this module
must NOT be gated behind ``pytest.importorskip``.

This intentionally goes further than the existing hermetic SC-5 coverage in
``tests/test_phase31_integration.py``:

  - The secret-shaped token driven through ``add_learning`` is assembled at
    runtime by concatenation (never a literal secret-looking string in this
    source file), proving the gate fires on the *shape* of the content, not
    on a hardcoded test fixture string.
  - The degraded-recall fake vector store below is *healthy* but its
    ``search()`` raises mid-process -- exercising the mid-process-failure
    branch of
    ``NativeMemoryProvider.recall()`` (the ``except Exception: degraded =
    True`` path), which the existing SC-5 test does not reach (it uses a
    vector store that is unhealthy from init, so ``search()`` is never
    called at all).

Production paths under test: ``src/brain_learn.py::add_learning``,
``src/brain_privacy.py::contains_pii_or_secret``,
``src/memory_provider.py::NativeMemoryProvider.recall``,
``src/memory.py::MemoryManager.get_relevant_memories``,
``core/constants.py::IDENTITY_MEMORY_INJECTION_CAP``.
"""
import asyncio
import logging
import string

import src.brain_index as bi
import src.brain_learn as bl


def run(coro):
    return asyncio.run(coro)


def _assemble_secret_token() -> str:
    """Build an OpenAI-key-shaped token (`sk-` + 24 alnum chars) purely by
    runtime concatenation -- no secret-looking literal appears verbatim in
    this source file. Deterministic (no randomness) so the test is stable."""
    prefix = "".join(["s", "k", "-"])
    alphabet = string.ascii_uppercase + string.digits
    suffix = "".join(alphabet[(i * 7 + 3) % len(alphabet)] for i in range(24))
    return prefix + suffix


def _brain(tmp_path):
    (tmp_path / "knowledge" / "code").mkdir(parents=True)
    return {"id": "t", "path": str(tmp_path), "dir": str(tmp_path)}


# ---------------------------------------------------------------------------
# Task 1 -- PII/secret-bearing lesson withheld from the shared brain, clean
# control lesson writes + indexes through the SAME real add_learning() path.
# ---------------------------------------------------------------------------

def test_pii_secret_lesson_withheld_from_shared_brain(tmp_path, monkeypatch, caplog):
    appended_calls = []

    def fake_append_learned(index_dir, note, embedder=None):
        appended_calls.append(note)

    monkeypatch.setattr(bi, "append_learned", fake_append_learned)

    # Explicitly enable shared auto-learning so it is the PII/secret gate --
    # not the private-by-default opt-in flag -- that blocks this write.
    import src.settings as settings
    monkeypatch.setattr(
        settings, "get_setting",
        lambda k, d=None: True if k == "brain_learning_enabled" else d,
    )
    assert bl.is_enabled() is True

    brain = _brain(tmp_path)
    secret_token = _assemble_secret_token()
    tainted_body = (
        "Our staging deploy script authenticates using the token "
        f"{secret_token} -- rotate it before this note leaves the runbook."
    )

    with caplog.at_level(logging.WARNING):
        withheld_ok = bl.add_learning(brain, "Staging deploy token", tainted_body)

    assert withheld_ok is False, "secret-bearing lesson must be withheld, not written"
    assert not (tmp_path / "knowledge" / "lessons").exists(), (
        "a withheld lesson must never cause knowledge/lessons/ to be created"
    )
    assert appended_calls == [], (
        "append_learned must never be called for a withheld lesson -- "
        "no partial index write on a fail-closed gate trip"
    )
    assert any("withheld" in r.message.lower() for r in caplog.records), (
        "a withheld lesson must log a WARNING naming the withhold"
    )

    # Control: a clean lesson through the SAME real add_learning() path DOES
    # write + index -- proves the gate is selective (secret/PII only), not a
    # blanket learning off-switch, and does not leak state from the withheld
    # attempt above.
    clean_ok = bl.add_learning(
        brain, "Atomic settings writes",
        "Always write settings JSON atomically so a crash mid-shutdown "
        "cannot corrupt the file on disk.",
    )
    assert clean_ok is True, "a clean control lesson must write through the same path"
    lessons = list((tmp_path / "knowledge" / "lessons").glob("*.md"))
    assert len(lessons) == 1, "the clean control lesson must be the only file written"
    assert len(appended_calls) == 1, "the clean control lesson must reach append_learned exactly once"
    assert appended_calls[0]["title"] == "Atomic settings writes"


# ---------------------------------------------------------------------------
# Task 2 -- degraded native recall (vector store healthy but search() raises
# mid-process) is tagged lexical/low_confidence, warns exactly once across
# repeated calls, and stays bounded by IDENTITY_MEMORY_INJECTION_CAP.
# ---------------------------------------------------------------------------

class FakeVectorStore:
    """A vector store that reports healthy=True (so NativeMemoryProvider
    enters the vector-search branch) but whose search() raises -- simulating
    a genuine mid-process failure (embedder crash, Chroma connection drop)
    rather than an unhealthy-from-init store. Exercises recall()'s
    `except Exception: degraded = True` branch."""

    def __init__(self):
        self.healthy = True
        self.added = []
        self.removed = []

    def add(self, memory_id, text):
        self.added.append((memory_id, text))

    def remove(self, memory_id):
        self.removed.append(memory_id)

    def search(self, query, k=5):
        raise RuntimeError("vector store search unavailable (simulated mid-process failure)")


def test_degraded_recall_tagged_lexical_warned_once_and_bounded(tmp_path, monkeypatch, caplog):
    from core.constants import IDENTITY_MEMORY_INJECTION_CAP
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider
    import src.memory_provider as memory_provider

    # Reset the module-level WARN-once latch so cross-test/process state
    # cannot mask a regression in the exactly-once assertion below.
    monkeypatch.setattr(memory_provider, "_lexical_fallback_warned", False)

    manager = MemoryManager(str(tmp_path))
    vector = FakeVectorStore()
    provider = NativeMemoryProvider(manager, vector)

    n_identity = IDENTITY_MEMORY_INJECTION_CAP + 2
    for i in range(n_identity):
        run(provider.remember(f"My name is Person{i}", owner="alice"))

    with caplog.at_level(logging.WARNING, logger="src.memory_provider"):
        first_hits = run(provider.recall("who am i", owner="alice", top_k=20))
        # A second recall call in the same process must add no further warning.
        second_hits = run(provider.recall("who am i", owner="alice", top_k=20))

    assert first_hits, "degraded recall must still return hits via the lexical fallback"
    assert second_hits, "the repeated recall call must also return hits"

    for hits in (first_hits, second_hits):
        assert all(h.mode == "lexical" for h in hits), "every hit must be tagged lexical when degraded"
        assert all(h.low_confidence is True for h in hits), "every degraded hit must be low_confidence"
        assert len(hits) <= IDENTITY_MEMORY_INJECTION_CAP, (
            f"identity injection must stay bounded at {IDENTITY_MEMORY_INJECTION_CAP} "
            f"(not top_k=20), got {len(hits)} from {n_identity} stored identity memories"
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        "exactly one WARNING must fire for the process across both recall() calls "
        f"(_warn_lexical_fallback_once latch), got {len(warnings)}"
    )
