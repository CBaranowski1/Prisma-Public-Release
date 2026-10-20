import asyncio
from src.chat_processor import ChatProcessor

class _P:
    provider_id="precision-audit"; enabled=True
    async def recall(self, q, *, owner=None, top_k=3):
        from src.memory_provider import MemoryRecord, MemorySearchHit
        return [MemorySearchHit(memory=MemoryRecord(id="1", text="Shard by tenant for scale"), provider_id="precision-audit", score=1.0)]
class _Reg:
    def active(self): return [_P()]

def test_brain_context_injects_and_tracks():
    cp = ChatProcessor.__new__(ChatProcessor)   # bypass heavy __init__
    cp.provider_registry = _Reg()
    msgs, used = asyncio.run(cp.brain_context("how do I scale?", owner=None))
    assert any("Shard by tenant" in m.get("content","") for m in msgs)
    assert used and used[0]["provider"] == "precision-audit"

def test_brain_context_no_registry():
    cp = ChatProcessor.__new__(ChatProcessor); cp.provider_registry = None
    msgs, used = asyncio.run(cp.brain_context("x"))
    assert msgs == [] and used == []


def test_brain_context_skips_combined_and_reports_only_injected_notes():
    from src.memory_provider import MemoryRecord, MemorySearchHit

    class Provider:
        def __init__(self, provider_id, score, text):
            self.provider_id = provider_id
            self.display_name = provider_id
            self.score = score
            self.text = text
            self.calls = 0

        async def recall(self, query, *, owner=None, top_k=3):
            self.calls += 1
            return [
                MemorySearchHit(
                    memory=MemoryRecord(id=self.provider_id, text=self.text),
                    provider_id=self.provider_id,
                    score=self.score,
                )
            ]

    low = Provider("brain:low", 0.1, "low result")
    high = Provider("brain:high", 0.9, "high result")
    combined = Provider("brain:all", 1.0, "duplicate combined result")
    cp = ChatProcessor.__new__(ChatProcessor)
    cp.provider_registry = type(
        "Registry",
        (),
        {"active": lambda self: [low, combined, high]},
    )()

    messages, used = asyncio.run(cp.brain_context("rank this", top_k=1))
    assert len(messages) == len(used) == 1
    assert used[0]["provider"] == "brain:high"
    assert "high result" in messages[0]["content"]
    assert combined.calls == 0
