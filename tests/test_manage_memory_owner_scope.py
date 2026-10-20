"""do_manage_memory (the live manage_memory tool in src.ai_interaction) per-user
CRUD isolation + vector-failure surfacing (GRAY-INT-05, primary reachable-path
proof). This is the actually-reachable implementation the live agent tool call
dispatches through (src.ai_interaction.dispatch_ai_tool), not
mcp_servers/memory_server.py which got the equivalent fix in 26-04.

Locks in: list/search never leak another owner's entries, cross-owner edit/
delete return a generic not-found (no data leak), a full single-owner CRUD
round-trip works, and a vector add failure is surfaced as a degraded marker
instead of a silent `except: pass`.
"""
import re

import pytest

from src.ai_interaction import set_memory_manager, do_manage_memory
from src.memory import MemoryManager


@pytest.fixture
def memory_fixture(tmp_path):
    """Wire a tmp_path-backed MemoryManager onto ai_interaction's module
    globals via the real set_memory_manager wiring hook."""
    manager = MemoryManager(str(tmp_path))
    set_memory_manager(manager, None)
    yield manager
    set_memory_manager(None, None)


def _extract_memory_id(result: dict) -> str:
    if result.get("memory_id"):
        return result["memory_id"]
    m = re.search(r"`([0-9a-f]+)`", result.get("results", ""))
    assert m, f"no memory id found in: {result}"
    return m.group(1)


async def test_list_isolation(memory_fixture):
    await do_manage_memory("add\nalice-secret", owner="alice")
    await do_manage_memory("add\nbob-secret", owner="bob")

    bob_list = await do_manage_memory("list", owner="bob")
    assert "alice-secret" not in bob_list.get("results", "")
    assert "bob-secret" in bob_list.get("results", "")

    alice_list = await do_manage_memory("list", owner="alice")
    assert "alice-secret" in alice_list.get("results", "")
    assert "bob-secret" not in alice_list.get("results", "")


async def test_cross_owner_edit_delete_not_found(memory_fixture):
    add_result = await do_manage_memory("add\nalice-secret", owner="alice")
    memory_id = _extract_memory_id(add_result)

    edit_result = await do_manage_memory(f"edit\n{memory_id}\nhacked", owner="bob")
    assert "error" in edit_result
    assert "not found" in edit_result["error"].lower()

    delete_result = await do_manage_memory(f"delete\n{memory_id}", owner="bob")
    assert "error" in delete_result
    assert "not found" in delete_result["error"].lower()

    alice_list = await do_manage_memory("list", owner="alice")
    assert "alice-secret" in alice_list.get("results", "")


async def test_full_roundtrip(memory_fixture):
    add_result = await do_manage_memory("add\nroundtrip-text", owner="alice")
    assert "error" not in add_result
    memory_id = _extract_memory_id(add_result)

    list_result = await do_manage_memory("list", owner="alice")
    assert "roundtrip-text" in list_result.get("results", "")

    edit_result = await do_manage_memory(f"edit\n{memory_id}\nroundtrip-updated", owner="alice")
    assert "error" not in edit_result
    assert "roundtrip-updated" in edit_result.get("results", "")

    delete_result = await do_manage_memory(f"delete\n{memory_id}", owner="alice")
    assert "error" not in delete_result

    final_list = await do_manage_memory("list", owner="alice")
    assert "roundtrip-updated" not in final_list.get("results", "")
    assert "No memories found" in final_list.get("results", "")


class _InMemoryVectorStore:
    """Faithful in-memory stand-in for MemoryVectorStore.

    Reproduces the ONE behavior that makes CR-01 a real bug: `add` is a no-op
    when the id already exists (mirrors MemoryVectorStore.add's "skip if already
    exists" guard). So an edit that only calls add() leaves the stale document
    in the index; a correct edit must remove() first, then add().
    """

    healthy = True

    def __init__(self):
        self.docs = {}  # id -> stored text (the "embedded" document)

    def add(self, memory_id, text):
        # No-op when the id already exists — exactly like MemoryVectorStore.add.
        if memory_id in self.docs:
            return
        self.docs[memory_id] = text

    def remove(self, memory_id):
        self.docs.pop(memory_id, None)


async def test_edit_refreshes_vector_index(memory_fixture):
    """Regression for CR-01: editing a memory must refresh the stored vector
    document, not leave the pre-edit text in the index.

    RED-before (edit called only add()): the no-op-on-existing-id add left the
    stale 'original-text' in the store. GREEN-after (edit does remove()+add()):
    the store reflects 'edited-text'.
    """
    vec = _InMemoryVectorStore()
    set_memory_manager(memory_fixture, vec)

    add_result = await do_manage_memory("add\noriginal-text", owner="alice")
    memory_id = _extract_memory_id(add_result)
    full_id = next(k for k in vec.docs if k.startswith(memory_id))
    assert vec.docs[full_id] == "original-text"

    edit_result = await do_manage_memory(f"edit\n{memory_id}\nedited-text", owner="alice")
    assert "error" not in edit_result

    # The stored embedding/document must now be the edited text, not the stale one.
    assert vec.docs[full_id] == "edited-text", (
        "vector index kept the pre-edit document — semantic recall would be stale"
    )


class _FailingVectorStore:
    healthy = True

    def add(self, memory_id, text):
        raise RuntimeError("vector backend unreachable")

    def remove(self, memory_id):
        raise RuntimeError("vector backend unreachable")


async def test_vector_failure_surfaced(memory_fixture, tmp_path):
    set_memory_manager(memory_fixture, _FailingVectorStore())
    result = await do_manage_memory("add\nno-relevant-keyword-here", owner="alice")
    assert "error" not in result
    text = result.get("results", "").lower()
    assert "vector" in text
    assert "fail" in text or "degrad" in text
