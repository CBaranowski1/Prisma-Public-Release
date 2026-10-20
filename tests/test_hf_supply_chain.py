"""Regression coverage for immutable Hugging Face downloads."""

import ast
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from routes.cookbook_routes import _resolve_hf_model_commit


REPO_ROOT = Path(__file__).resolve().parents[1]
PINNED_DOWNLOAD_FILES = (
    "scripts/add_hwfit_models.py",
    "scripts/build_codegen_corpus.py",
    "scripts/build_problem_corpus.py",
    "scripts/diffusion_server.py",
    "scripts/fetch_humaneval.py",
    "scripts/hf_download.py",
    "scripts/make_dataset.py",
)


@pytest.mark.asyncio
async def test_full_commit_revision_does_not_require_network():
    commit = "a" * 40
    assert await _resolve_hf_model_commit("org/model", commit, None) == commit


@pytest.mark.asyncio
async def test_invalid_revision_is_rejected_before_network():
    with pytest.raises(HTTPException) as exc:
        await _resolve_hf_model_commit("org/model", "main; rm -rf /", None)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_branch_is_resolved_to_hub_commit(monkeypatch):
    commit = "b" * 40

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sha": commit}

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            assert url.endswith("/org/model/revision/main")
            assert headers == {}
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert await _resolve_hf_model_commit("org/model", None, None) == commit


@pytest.mark.parametrize("relative_path", PINNED_DOWNLOAD_FILES)
def test_hub_download_calls_include_revision(relative_path):
    tree = ast.parse((REPO_ROOT / relative_path).read_text("utf-8"))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if name in {"load_dataset", "hf_hub_download", "snapshot_download", "_sd2"}:
            calls.append(node)

    assert calls, f"expected at least one Hub download in {relative_path}"
    for call in calls:
        assert any(kw.arg == "revision" for kw in call.keywords), (
            f"{relative_path}:{call.lineno} must pin a revision"
        )
