"""Phase 26 integration-checker connection (SC-5).

Proves the five GRAY-INT-26 fixes (26-01..06) on the *reachable* paths the
app actually runs — not only on the literally-named files — converting them
from "presence" to "integration":

  - image generation writes under a monkeypatched DATA_DIR through the
    reachable do_generate_image path (GRAY-INT-03/04)
  - the reachable do_manage_memory path round-trips per user with isolation
    between two owners (GRAY-INT-05)
  - a read-only (writable=False) provider degrades through
    MemoryService.remember instead of raising (GRAY-INT-13)
  - the startup search-shim identity audit passes — all 7 aliased
    (GRAY-INT-02, live)

Hermetic: no real image provider, no real Ollama, no network. Every helper
needed is replicated inline in this module (no cross-module fixture import).
"""
import base64
import re

import pytest

pytest.importorskip("mcp")

from src import ai_interaction  # noqa: E402
from src.memory import MemoryManager  # noqa: E402
from src.memory_provider import MemoryProvider  # noqa: E402

# A 1x1 transparent PNG, base64-encoded.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _FakeResponse:
    def __init__(self, status_code, json_data, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or str(json_data)

    def json(self):
        return self._json


class _CapturingAsyncClient:
    """Stand-in for httpx.AsyncClient supporting `async with ... as client`."""

    def __init__(self, response):
        self._response = response
        self.posted_url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kwargs):
        self.posted_url = url
        return self._response


@pytest.mark.asyncio
async def test_image_gen_writes_under_data_dir(monkeypatch, tmp_path):
    """do_generate_image (the reachable chat-image path) must write the
    generated PNG under a monkeypatched DATA_DIR, never under the process
    cwd."""
    import httpx

    monkeypatch.setattr("core.constants.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        ai_interaction,
        "_resolve_model",
        lambda model_spec, owner=None: (
            "https://api.openai.com/v1/chat/completions",
            "gpt-image-1",
            {"Authorization": "Bearer test"},
        ),
    )

    fake_response = _FakeResponse(200, {"data": [{"b64_json": _TINY_PNG_B64}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _CapturingAsyncClient(fake_response))

    fake_cwd = tmp_path / "_fake_cwd"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)

    result = await ai_interaction.do_generate_image("a cat\n")

    assert "error" not in result, result
    gen_dir = tmp_path / "generated_images"
    pngs = list(gen_dir.glob("*.png"))
    assert len(pngs) == 1, f"expected exactly one PNG under {gen_dir}, found {pngs}"
    assert base64.b64decode(_TINY_PNG_B64) == pngs[0].read_bytes()

    # No stray cwd-relative write.
    assert not (fake_cwd / "data" / "generated_images").exists()
    assert not (fake_cwd / "generated_images").exists()


def _extract_memory_id(result: dict) -> str:
    if result.get("memory_id"):
        return result["memory_id"]
    m = re.search(r"`([0-9a-f]+)`", result.get("results", ""))
    assert m, f"no memory id found in: {result}"
    return m.group(1)


@pytest.mark.asyncio
async def test_manage_memory_roundtrips_per_user(tmp_path):
    """do_manage_memory (the reachable manage_memory tool) round-trips per
    owner with isolation between two owners, and a cross-owner delete
    returns a generic not-found (no data leak)."""
    manager = MemoryManager(str(tmp_path))
    ai_interaction.set_memory_manager(manager, None)
    try:
        add_alice = await ai_interaction.do_manage_memory("add\nalice-only-memory", owner="alice")
        assert "error" not in add_alice
        alice_id = _extract_memory_id(add_alice)

        add_bob = await ai_interaction.do_manage_memory("add\nbob-only-memory", owner="bob")
        assert "error" not in add_bob

        alice_list = await ai_interaction.do_manage_memory("list", owner="alice")
        assert "alice-only-memory" in alice_list.get("results", "")
        assert "bob-only-memory" not in alice_list.get("results", "")

        bob_list = await ai_interaction.do_manage_memory("list", owner="bob")
        assert "bob-only-memory" in bob_list.get("results", "")
        assert "alice-only-memory" not in bob_list.get("results", "")

        # Cross-owner delete: bob cannot delete alice's memory — generic not-found.
        cross_delete = await ai_interaction.do_manage_memory(f"delete\n{alice_id}", owner="bob")
        assert "error" in cross_delete
        assert "not found" in cross_delete["error"].lower()

        # Alice's memory survives the failed cross-owner delete attempt.
        alice_list_after = await ai_interaction.do_manage_memory("list", owner="alice")
        assert "alice-only-memory" in alice_list_after.get("results", "")
    finally:
        ai_interaction.set_memory_manager(None, None)


class _ReadOnlyStubProvider(MemoryProvider):
    """A read-only stub provider (writable=False) whose remember() would
    raise if ever called — proves MemoryService.remember degrades via the
    writable guard rather than propagating the raise."""

    provider_id = "readonly-integration-stub"
    display_name = "Read-only integration stub"
    writable = False

    async def remember(self, *a, **k):
        raise NotImplementedError("read-only stub must never be called")

    async def recall(self, query, **kwargs):
        return []

    async def list_memories(self, **kwargs):
        return []

    async def delete(self, memory_id, **kwargs):
        return False


@pytest.mark.asyncio
async def test_readonly_provider_degrades(tmp_path):
    """A read-only provider degrades through MemoryService.remember instead
    of raising, and stores nothing."""
    from services.memory.service import MemoryService

    svc = MemoryService(str(tmp_path))
    svc.provider = _ReadOnlyStubProvider()

    result = await svc.remember("this should never be stored")

    assert result is None
    assert svc.get_all() == []


def test_search_shim_audit_live():
    """The startup search-shim identity audit passes live — all 7 src.search.*
    compat shims are still sys.modules-aliased to their services.search.*
    canonical counterparts (GRAY-INT-02)."""
    from src.search import audit_search_shim_identity

    audit_search_shim_identity()
