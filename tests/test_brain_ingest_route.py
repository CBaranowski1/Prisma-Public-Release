import os, importlib
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(vault: Path, monkeypatch):
    monkeypatch.setenv("PRISMA_BRAIN_VAULT", str(vault))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    import routes.brain_ingest_routes as ir
    importlib.reload(ir)
    app = FastAPI()
    app.include_router(ir.setup_brain_ingest_routes(auth_manager=None))
    return TestClient(app)


class _FakeAuth:
    """Minimal auth_manager stand-in for exercising the can_manage_brain gate."""
    def __init__(self, privs_by_user):
        self._p = privs_by_user

    def get_username_for_token(self, token):
        return None  # no cookie sessions in these tests

    def get_privileges(self, user):
        return self._p.get(user, {})


def _bearer_client(vault: Path, owner: str, privs: dict, monkeypatch):
    """App whose middleware simulates a bearer-token request (sets api_token +
    api_token_owner, current_user='api'), with a fake auth_manager."""
    monkeypatch.setenv("PRISMA_BRAIN_VAULT", str(vault))
    import routes.brain_ingest_routes as ir
    importlib.reload(ir)
    app = FastAPI()

    @app.middleware("http")
    async def _set_bearer_state(request, call_next):
        request.state.api_token = True
        request.state.api_token_owner = owner
        request.state.current_user = "api"
        return await call_next(request)

    app.include_router(ir.setup_brain_ingest_routes(auth_manager=_FakeAuth(privs)))
    return TestClient(app)


def test_bearer_owner_with_privilege_can_ingest(tmp_path, monkeypatch):
    c = _bearer_client(tmp_path, "alice", {"alice": {"can_manage_brain": True}}, monkeypatch)
    j = c.post("/api/brain/ingest", json={"filename": "n.md", "content": "# N\nbody"}).json()
    assert "results" in j and j["results"][0]["status"] == "created"   # NOT forbidden


def test_bearer_owner_without_privilege_forbidden(tmp_path, monkeypatch):
    c = _bearer_client(tmp_path, "bob", {"bob": {"can_manage_brain": False}}, monkeypatch)
    j = c.post("/api/brain/ingest", json={"filename": "n.md", "content": "# N\nbody"}).json()
    assert "forbidden" in (j.get("error") or "")


def test_ingest_json_writes_note(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/brain/ingest", json={
        "filename": "scrape1.md",
        "content": "# SQL Injection\nA security vulnerability. [[input-validation]]\n",
    })
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["status"] == "created" and res["category"] == "security"
    p = tmp_path / "knowledge" / "security" / (res["slug"] + ".md")
    assert p.is_file() and "title: SQL Injection" in p.read_text(encoding="utf-8")


def test_explicit_category_and_collision_suffix(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    body = {"filename": "n.md", "content": "# Note\nbody", "category": "python"}
    a = c.post("/api/brain/ingest", json=body).json()["results"][0]
    b = c.post("/api/brain/ingest", json=body).json()["results"][0]
    assert a["category"] == "python" and a["status"] == "created"
    assert b["status"] == "renamed" and b["slug"] != a["slug"]
    assert (tmp_path / "knowledge" / "python" / (a["slug"] + ".md")).is_file()
    assert (tmp_path / "knowledge" / "python" / (b["slug"] + ".md")).is_file()


def test_rejects_bad_category(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/brain/ingest", json={"filename": "x.md", "content": "# X\nb",
                                          "category": "../../evil"})
    res = r.json()["results"][0]
    assert "/.." not in res["path"] and res["category"] != "../../evil"


def test_no_vault_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISMA_BRAIN_VAULT", "")
    import routes.brain_ingest_routes as ir, importlib
    importlib.reload(ir)
    app = FastAPI(); app.include_router(ir.setup_brain_ingest_routes(auth_manager=None))
    r = TestClient(app).post("/api/brain/ingest", json={"filename": "x.md", "content": "# X"})
    assert r.json().get("error")
