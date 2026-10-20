"""memory_server per-user CRUD isolation + vector-failure surfacing (GRAY-INT-05).

mcp_servers/memory_server.py currently ignores any `owner` in the tool arguments:
list/search read via an unscoped load(), edit/delete never check ownership before
mutating, and vector add/remove failures are swallowed by a bare
`except Exception: pass`. This proves per-user isolation (add/list), a generic
"not found" on cross-owner edit/delete (no leak of the other owner's data), a
full single-owner CRUD round-trip, and a surfaced vector-failure marker before
the fix lands.
"""
import re

import pytest

pytest.importorskip("mcp")

import mcp_servers.memory_server as ms
from src.memory import MemoryManager


@pytest.fixture
def memory_server(tmp_path, monkeypatch):
    """Wire a tmp_path-backed MemoryManager onto memory_server's module globals,
    bypassing _ensure_init so the real DATA_DIR is never touched."""
    manager = MemoryManager(str(tmp_path))
    monkeypatch.setattr(ms, "_memory_manager", manager)
    monkeypatch.setattr(ms, "_memory_vector", None)
    monkeypatch.setattr(ms, "_initialized", True)
    return ms


def _extract_id(text: str) -> str:
    m = re.search(r"\(id: ([0-9a-f]+)\)", text)
    assert m, f"no id found in: {text}"
    return m.group(1)


async def test_add_list_isolation(memory_server):
    await memory_server.call_tool(
        "manage_memory", {"action": "add", "text": "alice-secret", "owner": "alice"}
    )
    await memory_server.call_tool(
        "manage_memory", {"action": "add", "text": "bob-secret", "owner": "bob"}
    )

    bob_list = await memory_server.call_tool("manage_memory", {"action": "list", "owner": "bob"})
    assert "alice-secret" not in bob_list[0].text
    assert "bob-secret" in bob_list[0].text

    alice_list = await memory_server.call_tool("manage_memory", {"action": "list", "owner": "alice"})
    assert "alice-secret" in alice_list[0].text
    assert "bob-secret" not in alice_list[0].text


async def test_cross_owner_edit_delete_not_found(memory_server):
    add_result = await memory_server.call_tool(
        "manage_memory", {"action": "add", "text": "alice-secret", "owner": "alice"}
    )
    memory_id = _extract_id(add_result[0].text)

    edit_result = await memory_server.call_tool(
        "manage_memory", {"action": "edit", "memory_id": memory_id, "text": "hacked", "owner": "bob"}
    )
    assert "not found" in edit_result[0].text.lower()
    assert "alice-secret" not in edit_result[0].text
    assert "hacked" not in edit_result[0].text

    delete_result = await memory_server.call_tool(
        "manage_memory", {"action": "delete", "memory_id": memory_id, "owner": "bob"}
    )
    assert "not found" in delete_result[0].text.lower()
    assert "alice-secret" not in delete_result[0].text

    alice_list = await memory_server.call_tool("manage_memory", {"action": "list", "owner": "alice"})
    assert "alice-secret" in alice_list[0].text


async def test_full_roundtrip_single_owner(memory_server):
    add_result = await memory_server.call_tool(
        "manage_memory", {"action": "add", "text": "roundtrip-text", "owner": "alice"}
    )
    memory_id = _extract_id(add_result[0].text)

    list_result = await memory_server.call_tool("manage_memory", {"action": "list", "owner": "alice"})
    assert "roundtrip-text" in list_result[0].text

    edit_result = await memory_server.call_tool(
        "manage_memory",
        {"action": "edit", "memory_id": memory_id, "text": "roundtrip-updated", "owner": "alice"},
    )
    assert "roundtrip-updated" in edit_result[0].text

    delete_result = await memory_server.call_tool(
        "manage_memory", {"action": "delete", "memory_id": memory_id, "owner": "alice"}
    )
    assert "deleted" in delete_result[0].text.lower()

    final_list = await memory_server.call_tool("manage_memory", {"action": "list", "owner": "alice"})
    assert "roundtrip-updated" not in final_list[0].text


class _FailingVectorStore:
    healthy = True

    def add(self, memory_id, text):
        raise RuntimeError("vector backend unreachable")

    def remove(self, memory_id):
        raise RuntimeError("vector backend unreachable")


async def test_vector_failure_surfaced(memory_server, monkeypatch):
    monkeypatch.setattr(ms, "_memory_vector", _FailingVectorStore())
    result = await memory_server.call_tool(
        "manage_memory", {"action": "add", "text": "no-relevant-keyword-here", "owner": "alice"}
    )
    text = result[0].text.lower()
    assert "vector" in text
    assert "fail" in text or "degrad" in text
