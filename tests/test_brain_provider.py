import asyncio
from src.memory_provider import MemoryProvider, BrainDoc
from src.brain_provider import GuardedProvider, discover_providers


class _Min(MemoryProvider):
    provider_id = "min"
    display_name = "Min"

    async def remember(self, *a, **k): ...
    async def recall(self, *a, **k): return []
    async def list_memories(self, *a, **k): return []
    async def delete(self, *a, **k): return False


def test_search_and_categories_default_empty():
    p = _Min()
    assert asyncio.run(p.search("q")) == []
    assert asyncio.run(p.categories()) == []


def test_braindoc_fields():
    d = BrainDoc(id="1", title="T", snippet="s", category="c", source_path="p", score=0.5)
    assert d.title == "T" and d.score == 0.5


class _Boom(_Min):
    provider_id = "boom"
    async def recall(self, *a, **k): raise RuntimeError("kaboom")


def test_guard_swallows_and_degrades():
    g = GuardedProvider(_Boom(), timeout_s=1.0)
    assert asyncio.run(g.recall("q")) == []      # never raises
    assert g.healthy is False
    assert "kaboom" in (g.last_error or "")


def test_guard_passes_through_ok():
    g = GuardedProvider(_Min())
    assert asyncio.run(g.recall("q")) == []
    assert g.healthy is True


def test_discover_finds_subclasses(tmp_path):
    f = tmp_path / "demo_provider.py"
    f.write_text(
        "from src.memory_provider import MemoryProvider\n"
        "class Demo(MemoryProvider):\n"
        "    provider_id='demo'; display_name='Demo'\n"
        "    async def remember(self,*a,**k): ...\n"
        "    async def recall(self,*a,**k): return []\n"
        "    async def list_memories(self,*a,**k): return []\n"
        "    async def delete(self,*a,**k): return False\n")
    found = discover_providers(str(tmp_path))
    assert any(p.provider_id == 'demo' for p in found)
