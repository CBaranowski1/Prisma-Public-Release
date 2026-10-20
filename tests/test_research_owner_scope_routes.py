"""Route-level owner-scope tests for persisted research reports."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

# Bind the module object once and use it for BOTH the DATA_DIR patch and the
# route setup. A bare `from ... import setup_research_routes` + string-path
# monkeypatch breaks if another test importlib.reload()s research_routes: the
# patch would target the new sys.modules object while the imported function
# still reads the old module's DATA_DIR global. Going through one object avoids
# that mismatch.
import routes.research_routes as _rr


class _Auth:
    is_configured = True

    def __init__(self, can_use_research=True):
        self.can_use_research = can_use_research

    def get_privileges(self, _user):
        return {"can_use_research": self.can_use_research}


def _request(user: str, *, can_use_research=True):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
        app=SimpleNamespace(
            state=SimpleNamespace(auth_manager=_Auth(can_use_research))
        ),
    )


def _route(router, path: str, method: str):
    for route in router.routes:
        if getattr(route, "path", "") != path:
            continue
        if method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"{method} {path} route not registered")


def _write_research(data_dir, session_id: str, **data):
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{session_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _research_handler():
    handler = MagicMock()
    handler._active_tasks = {}
    return handler


def test_library_returns_only_caller_owned_unarchived_reports(tmp_path, monkeypatch):
    # research_routes resolves report paths under the frozen-aware DATA_DIR
    # (not the cwd), so point that at the temp dir rather than chdir-ing.
    monkeypatch.setattr(_rr, "DATA_DIR", str(tmp_path / "data"))
    data_dir = tmp_path / "data" / "deep_research"
    _write_research(data_dir, "alice-live", owner="alice", query="Alice", completed_at=30)
    _write_research(data_dir, "alice-archived", owner="alice", query="Archived", archived=True)
    _write_research(data_dir, "bob-live", owner="bob", query="Bob", completed_at=40)
    _write_research(data_dir, "legacy-null", query="Legacy", completed_at=50)

    router = _rr.setup_research_routes(_research_handler())
    target = _route(router, "/api/research/library", "GET")

    out = asyncio.run(target(
        request=_request("alice"),
        search=None,
        sort="recent",
        limit=50,
        archived=False,
    ))

    assert [item["id"] for item in out["research"]] == ["alice-live"]
    assert out["total"] == 1


def test_library_denied_when_research_privilege_revoked(tmp_path, monkeypatch):
    monkeypatch.setattr(_rr, "DATA_DIR", str(tmp_path / "data"))
    router = _rr.setup_research_routes(_research_handler())
    target = _route(router, "/api/research/library", "GET")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(
            request=_request("alice", can_use_research=False),
            search=None,
            sort="recent",
            limit=50,
            archived=False,
        ))

    assert exc.value.status_code == 403


def test_detail_rejects_cross_owner_and_null_owner_reports(tmp_path, monkeypatch):
    # research_routes resolves report paths under the frozen-aware DATA_DIR
    # (not the cwd), so point that at the temp dir rather than chdir-ing.
    monkeypatch.setattr(_rr, "DATA_DIR", str(tmp_path / "data"))
    data_dir = tmp_path / "data" / "deep_research"
    _write_research(data_dir, "bob-report", owner="bob", result="bob secret")
    _write_research(data_dir, "legacy-report", result="legacy secret")

    router = _rr.setup_research_routes(_research_handler())
    target = _route(router, "/api/research/detail/{session_id}", "GET")

    for session_id in ("bob-report", "legacy-report"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(target(session_id=session_id, request=_request("alice")))
        assert exc.value.status_code == 404


def test_report_rejects_null_owner_before_generating_html(tmp_path, monkeypatch):
    # research_routes resolves report paths under the frozen-aware DATA_DIR
    # (not the cwd), so point that at the temp dir rather than chdir-ing.
    monkeypatch.setattr(_rr, "DATA_DIR", str(tmp_path / "data"))
    data_dir = tmp_path / "data" / "deep_research"
    _write_research(data_dir, "legacy-report", result="legacy secret")

    handler = _research_handler()
    router = _rr.setup_research_routes(handler)
    target = _route(router, "/api/research/report/{session_id}", "GET")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="legacy-report", request=_request("alice")))

    assert exc.value.status_code == 404
    handler.get_report_html.assert_not_called()


def test_archive_rejects_cross_owner_without_mutating_report(tmp_path, monkeypatch):
    # research_routes resolves report paths under the frozen-aware DATA_DIR
    # (not the cwd), so point that at the temp dir rather than chdir-ing.
    monkeypatch.setattr(_rr, "DATA_DIR", str(tmp_path / "data"))
    data_dir = tmp_path / "data" / "deep_research"
    path = _write_research(data_dir, "bob-report", owner="bob", archived=False)

    router = _rr.setup_research_routes(_research_handler())
    target = _route(router, "/api/research/{session_id}/archive", "POST")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="bob-report", request=_request("alice"), archived=True))

    assert exc.value.status_code == 404
    assert json.loads(path.read_text(encoding="utf-8"))["archived"] is False


def test_delete_rejects_cross_owner_without_unlinking_report(tmp_path, monkeypatch):
    # research_routes resolves report paths under the frozen-aware DATA_DIR
    # (not the cwd), so point that at the temp dir rather than chdir-ing.
    monkeypatch.setattr(_rr, "DATA_DIR", str(tmp_path / "data"))
    data_dir = tmp_path / "data" / "deep_research"
    path = _write_research(data_dir, "bob-report", owner="bob", result="bob secret")

    router = _rr.setup_research_routes(_research_handler())
    target = _route(router, "/api/research/{session_id}", "DELETE")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(target(session_id="bob-report", request=_request("alice")))

    assert exc.value.status_code == 404
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["result"] == "bob secret"
