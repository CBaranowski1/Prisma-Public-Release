"""TDD tests for brain-note injection into council seat prompts (Task 13)."""

import asyncio
from src.council.schemas import Seat
from src.council import seat_runner


class _FakeReg:
    def active(self):
        class P:
            provider_id = "precision-audit"

            async def recall(self, q, *, owner=None, top_k=3):
                from src.memory_provider import MemoryRecord, MemorySearchHit
                return [MemorySearchHit(
                    memory=MemoryRecord(id="1", text="Partitioning helps ingest"),
                    provider_id="precision-audit",
                    score=1.0,
                )]
        return [P()]


def test_brain_notes_appended_to_user_message():
    seat = Seat(id="a", name="A", role="panelist")
    msgs = asyncio.run(seat_runner.build_messages_with_brain(
        seat, "How do I scale ingest?", [], _FakeReg()))
    joined = " ".join(m["content"] for m in msgs)
    assert "Partitioning helps ingest" in joined


def test_no_registry_is_plain_build():
    seat = Seat(id="a", name="A", role="panelist")
    msgs = asyncio.run(seat_runner.build_messages_with_brain(seat, "hi", [], None))
    # identical to the plain builder when there's no registry
    assert msgs == seat_runner._build_messages(seat, "hi", [])
