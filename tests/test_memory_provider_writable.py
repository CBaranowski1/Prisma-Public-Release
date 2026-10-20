"""Tests for the `writable` capability flag on the MemoryProvider contract.

A read-only provider (writable=False) must let callers degrade gracefully
(logged skip, no store) instead of crashing on the existing
`raise NotImplementedError` in its `remember()` body. Covers both guarded
call sites: MemoryService.remember and the episodic write path.
"""

import asyncio

from src.memory_provider import MemoryProvider, MemoryRecord


def run(coro):
    return asyncio.run(coro)


class ReadOnlyStubProvider(MemoryProvider):
    """A read-only provider: writable=False, remember() raises but also
    counts calls so tests can prove a pre-check skipped it entirely."""

    provider_id = "readonly-stub"
    display_name = "Read-only stub"
    writable = False

    def __init__(self):
        self.remember_calls = 0

    async def remember(self, *a, **k):
        self.remember_calls += 1
        raise NotImplementedError("read-only stub")

    async def recall(self, query, **kwargs):
        return []

    async def list_memories(self, **kwargs):
        return []

    async def delete(self, memory_id, **kwargs):
        return False


class WritableStubProvider(MemoryProvider):
    """A normal writable provider (writable defaults to True via the base)."""

    provider_id = "writable-stub"
    display_name = "Writable stub"

    async def remember(self, *a, **k):
        return MemoryRecord(id="x", text="stub")

    async def recall(self, query, **kwargs):
        return []

    async def list_memories(self, **kwargs):
        return []

    async def delete(self, memory_id, **kwargs):
        return False


def test_memoryservice_remember_degrades(tmp_path):
    from services.memory.service import MemoryService

    svc = MemoryService(str(tmp_path))
    svc.provider = ReadOnlyStubProvider()

    result = run(svc.remember("x"))

    assert result is None
    assert svc.get_all() == []


def test_episodic_skips_readonly():
    from src.episodic_memory import write_episode

    stub = ReadOnlyStubProvider()

    class FakeRegistry:
        def active(self):
            return [stub]

    episode = {
        "title": "test lesson",
        "trigger": "test trigger",
        "procedure": "did the thing",
        "source_turn_id": "turn-1",
        "ts": 0,
        "source": "episodic",
    }

    result = run(write_episode(FakeRegistry(), episode))

    assert result is None
    # Proves the writable pre-check skipped this provider before remember()
    # was ever entered — not just the except-NotImplementedError fallback.
    assert stub.remember_calls == 0


def test_base_default_writable_true(tmp_path):
    from src.memory import MemoryManager
    from src.memory_provider import NativeMemoryProvider

    assert MemoryProvider.writable is True

    manager = MemoryManager(str(tmp_path))
    provider = NativeMemoryProvider(manager)
    assert provider.writable is True


def test_guarded_proxies_writable():
    from src.brain_provider import GuardedProvider

    writable_wrap = GuardedProvider(WritableStubProvider())
    readonly_wrap = GuardedProvider(ReadOnlyStubProvider())

    assert writable_wrap.writable is True
    assert readonly_wrap.writable is False
