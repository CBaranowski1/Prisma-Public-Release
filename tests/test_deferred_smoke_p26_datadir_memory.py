"""Phase 37 automated re-encoding of Phase 26's deferred live smoke.

Source of the deferred property: 26-07 Task 2 / 26-VERIFICATION.md's "Human
Verification Required" section — start the dev server, confirm no shim-drift
RuntimeError at boot, generate an image and confirm it lands under the
configured DATA_DIR (not a stray cwd-relative data/ folder), add/list a
memory as one user, and confirm no crash occurs against a read-only
brain/vault provider.

This file is a STRONGER, assembled-app-level proof than
`tests/test_phase26_integration.py`:
  - the boot-audit test drives the REAL FastAPI app + lifespan via
    `fastapi.testclient.TestClient`, not a bare function call
    (`tests/test_phase26_integration.py::test_search_shim_audit_live` only
    calls `audit_search_shim_identity()` directly — it never proves the
    startup wiring in `app.py::_startup_event` actually invokes it);
  - the per-user isolation test drives the assembled memory ROUTE handlers
    (`routes/memory_routes.py`, via `request.state.current_user`) the way a
    real browser session would, instead of only the `do_manage_memory` MCP
    tool function the hermetic test exercises.

It must NOT merely duplicate `tests/test_phase26_integration.py` — the image
write and read-only-degrade tests below mirror that file's proven fixture
mechanics (fake httpx client, tmp_path DATA_DIR, monkeypatch.chdir) because
those mechanics are how the underlying property is faithfully reproduced,
but the assembled-app boot audit and the route-layer isolation test are new
proof surfaces this file adds.

Hermetic: no real image provider, no real Ollama, no network. Every helper
needed is replicated inline in this module (no cross-module fixture import).
"""
import asyncio
import base64

import pytest

pytest.importorskip("mcp")

from src import ai_interaction  # noqa: E402
from src.memory import MemoryManager  # noqa: E402
from src.memory_provider import MemoryProvider  # noqa: E402

# A 1x1 transparent PNG, base64-encoded.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


# ---------------------------------------------------------------------------
# Test 1: boot-time search-shim identity audit via the assembled app lifespan
# ---------------------------------------------------------------------------


def test_boot_audit_runs_via_assembled_app_lifespan(monkeypatch, tmp_path):
    """The startup search-shim identity audit must run as part of the REAL
    app's lifespan (not merely be callable in isolation) so a future
    shim-drift regression fails loudly at boot (GRAY-INT-02, live).

    Uses `fastapi.testclient.TestClient` as a context manager, which drives
    Starlette's real lifespan protocol (`app.router.lifespan_context`) --
    this is the assembled surface `app.py::_startup_event` actually runs
    under, not a bare function call. `PRISMA_DISABLE_MCP` keeps this
    hermetic (no subprocess MCP servers spawned during boot).
    """
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PRISMA_DISABLE_MCP", "1")

    from fastapi.testclient import TestClient
    import app as appmod

    # If the search-shim identity audit ever drifts, `_startup_event()`
    # raises RuntimeError uncaught, which propagates out of the lifespan
    # and fails this `with` block loudly -- exactly the boot-time property
    # the deferred live smoke was meant to prove. No try/except here on
    # purpose: a regression must fail this test, not be swallowed.
    with TestClient(appmod.app) as client:
        resp = client.get("/api/ready")

    assert resp.status_code == 200, resp.text
    assert resp.json().get("ready") is True, resp.text


# ---------------------------------------------------------------------------
# Test 2: generated image lands under the configured DATA_DIR
# ---------------------------------------------------------------------------


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
async def test_image_gen_writes_under_configured_data_dir_not_cwd(monkeypatch, tmp_path):
    """do_generate_image (the reachable chat-image path) must write the
    generated PNG under a monkeypatched DATA_DIR, never under the process
    cwd -- the exact property 26-07 Task 2's live smoke asked a human to
    eyeball via a real chat-driven image generation."""
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


# ---------------------------------------------------------------------------
# Test 3: a read-only provider degrades through MemoryService.remember
# ---------------------------------------------------------------------------


class _ReadOnlyStubProvider(MemoryProvider):
    """A read-only stub provider (writable=False) whose remember() would
    raise if ever called -- proves MemoryService.remember degrades via the
    writable guard rather than propagating the raise, and stores nothing."""

    provider_id = "readonly-deferred-smoke-stub"
    display_name = "Read-only deferred-smoke stub"
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
async def test_readonly_provider_degrades_through_memory_service(tmp_path):
    """A read-only (writable=False) provider degrades through
    MemoryService.remember instead of crashing -- 26-07 Task 2's live smoke
    asked a human to confirm "no crash occurs against a read-only
    brain/vault provider"; this proves the same degrade path in-process."""
    from services.memory.service import MemoryService

    svc = MemoryService(str(tmp_path))
    svc.provider = _ReadOnlyStubProvider()

    result = await svc.remember("this should never be stored")

    assert result is None
    assert svc.get_all() == []


# ---------------------------------------------------------------------------
# Test 4: per-user memory isolation through the assembled route handlers
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from routes.memory_routes import setup_memory_routes  # noqa: E402
from src.request_models import MemoryAddRequest  # noqa: E402


def _route(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") != path:
            continue
        if method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def _mem_request(user: str):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(state=SimpleNamespace()),
    )


def test_per_user_memory_isolation_through_assembled_memory_routes(tmp_path):
    """Prove per-user memory isolation at the ROUTE layer -- the assembled
    surface a real browser session hits (`request.state.current_user`) --
    rather than only through `do_manage_memory` as
    `tests/test_phase26_integration.py::test_manage_memory_roundtrips_per_user`
    already does. Mirrors the synthetic-authenticated-request pattern from
    `tests/test_research_owner_scope_routes.py`.

    `session_manager` is a MagicMock: none of the routes exercised here
    (`add`, list, get-by-id, delete) touch it, so it is a non-load-bearing
    dependency per the plan's wiring guidance.
    """
    manager = MemoryManager(str(tmp_path))
    session_manager = MagicMock()
    router = setup_memory_routes(manager, session_manager, memory_vector=None)

    add_route = _route(router, "/api/memory/add", "POST")
    list_route = _route(router, "/api/memory", "GET")
    get_item_route = _route(router, "/api/memory/{memory_id}", "GET")
    delete_route = _route(router, "/api/memory/{memory_id}", "DELETE")

    # Alice adds a memory via the assembled add-endpoint.
    add_alice = asyncio.run(add_route(
        request=_mem_request("alice"),
        memory_data=MemoryAddRequest(text="alice-only-memory", category="fact", source="user"),
    ))
    assert add_alice["ok"] is True

    # Bob adds a different memory.
    add_bob = asyncio.run(add_route(
        request=_mem_request("bob"),
        memory_data=MemoryAddRequest(text="bob-only-memory", category="fact", source="user"),
    ))
    assert add_bob["ok"] is True

    alice_id = next(m["id"] for m in manager.load(owner="alice") if m["text"] == "alice-only-memory")
    bob_id = next(m["id"] for m in manager.load(owner="bob") if m["text"] == "bob-only-memory")
    assert alice_id != bob_id

    # The list/search endpoint invoked as alice surfaces alice's memory and
    # NOT bob's.
    alice_view = list_route(request=_mem_request("alice"))
    alice_texts = [m["text"] for m in alice_view["memory"]]
    assert "alice-only-memory" in alice_texts
    assert "bob-only-memory" not in alice_texts

    # Invoked as bob surfaces bob's and NOT alice's.
    bob_view = list_route(request=_mem_request("bob"))
    bob_texts = [m["text"] for m in bob_view["memory"]]
    assert "bob-only-memory" in bob_texts
    assert "alice-only-memory" not in bob_texts

    # A cross-owner GET (bob targeting alice's id) is denied with a generic
    # not-found -- no data leak about whether the memory exists at all.
    with pytest.raises(HTTPException) as exc:
        get_item_route(request=_mem_request("bob"), memory_id=alice_id)
    assert exc.value.status_code == 404

    # A cross-owner DELETE (bob targeting alice's id) is also denied.
    with pytest.raises(HTTPException) as exc:
        delete_route(request=_mem_request("bob"), memory_id=alice_id)
    assert exc.value.status_code == 404

    # Alice's memory survives the failed cross-owner delete attempt.
    alice_after = list_route(request=_mem_request("alice"))
    alice_texts_after = [m["text"] for m in alice_after["memory"]]
    assert "alice-only-memory" in alice_texts_after
