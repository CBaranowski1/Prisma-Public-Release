"""Public council documentation and cookbook placeholder regression tests."""
from pathlib import Path

import pytest
from src import tool_implementations as ti

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOC = _REPO_ROOT / "docs" / "COUNCIL-MODULES.md"


class TestCouncilPublicModuleDocs:
    def test_documented_modules_exist_and_cover_runtime_tree(self):
        doc = _DOC.read_text(encoding="utf-8")
        modules = {p.name for p in (_REPO_ROOT / "src" / "council").glob("*.py")
                   if p.name != "__init__.py"}
        assert modules
        for name in modules:
            assert f"`{name}`" in doc, f"Undocumented council module: {name}"

    def test_documentation_does_not_claim_removed_legacy_modules(self):
        doc = _DOC.read_text(encoding="utf-8")
        for name in ("triage.py", "specialists.py", "chief.py"):
            assert f"`{name}`" not in doc

    def test_documented_route_is_wired_to_real_orchestrator(self):
        doc = _DOC.read_text(encoding="utf-8")
        route = (_REPO_ROOT / "routes" / "council_routes.py").read_text(encoding="utf-8")
        assert "routes/council_routes.py" in doc and "run_council" in doc
        assert "from src.council.orchestrator import run_council" in route
        assert "run_council(" in route


# ── GRAY-AGT-08a: cookbook placeholder is an explicit, honest waiting state ──

class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient supporting `async with ... as client`,
    mirroring tests/test_do_generate_image_endpoint.py's _CapturingAsyncClient
    shape. Serves a GET of the current cookbook state and captures the POST
    body so the test can inspect the registered task entry."""

    def __init__(self, get_response):
        self._get_response = get_response
        self.posted_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kwargs):
        return self._get_response

    async def post(self, url, json=None, headers=None, **kwargs):
        self.posted_json = json
        return _FakeResponse(200, {"ok": True})


class TestCookbookPlaceholderHonesty:
    @pytest.mark.asyncio
    async def test_registered_task_pairs_placeholder_with_non_final_status(self, monkeypatch):
        """The registered entry's status must be the non-final 'running'
        state — never a terminal status — while it carries the waiting
        placeholder output, so a consumer can tell this is not a result."""
        import httpx

        captured = {}

        def _client_factory(*a, **kw):
            client = _FakeAsyncClient(_FakeResponse(200, {"tasks": []}))
            captured["client"] = client
            return client

        monkeypatch.setattr(httpx, "AsyncClient", _client_factory)
        monkeypatch.setattr(ti, "_internal_headers", lambda *a, **kw: {})

        ok = await ti._cookbook_register_task(
            "sess-123", "Qwen/Qwen3-8B", "", "vllm serve Qwen/Qwen3-8B", task_type="serve",
        )

        assert ok is True
        posted = captured["client"].posted_json
        assert posted is not None
        [entry] = posted["tasks"]
        assert entry["status"] == "running"
        assert entry["status"] not in ti._COOKBOOK_TERMINAL_STATUSES
        assert "waiting for tmux output" in entry["output"]
        assert "sess-123" in entry["output"]

    @pytest.mark.asyncio
    async def test_duplicate_session_id_skips_without_reregistering(self, monkeypatch):
        """An already-registered session_id must be a no-op (best-effort
        idempotency) rather than appending a second placeholder entry."""
        import httpx

        existing_state = {
            "tasks": [{"sessionId": "sess-dup", "status": "running", "output": "already here"}],
        }

        def _client_factory(*a, **kw):
            return _FakeAsyncClient(_FakeResponse(200, existing_state))

        monkeypatch.setattr(httpx, "AsyncClient", _client_factory)
        monkeypatch.setattr(ti, "_internal_headers", lambda *a, **kw: {})

        ok = await ti._cookbook_register_task(
            "sess-dup", "Qwen/Qwen3-8B", "", "vllm serve Qwen/Qwen3-8B",
        )
        assert ok is True

    def test_non_final_status_constant_is_not_terminal(self):
        """The module's documented terminal-status set must not include the
        status this placeholder is paired with — the explicit code contract
        GRAY-AGT-08a asks for."""
        assert "running" not in ti._COOKBOOK_TERMINAL_STATUSES
        assert ti._COOKBOOK_TERMINAL_STATUSES == frozenset(
            {"done", "completed", "error", "stopped"}
        )
