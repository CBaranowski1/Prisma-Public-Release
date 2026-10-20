import os, tempfile
import pytest
from core.auth import AuthManager, DEFAULT_PRIVILEGES


@pytest.fixture
def mgr():
    d = tempfile.mkdtemp()
    return AuthManager(os.path.join(d, "auth.json"))


def test_new_privilege_flags_exist_and_default_false():
    assert DEFAULT_PRIVILEGES["can_manage_brain"] is False
    assert DEFAULT_PRIVILEGES["can_manage_models"] is False


def test_create_user_default_role_is_user(mgr):
    mgr.create_user("bob", "password123")
    row = next(u for u in mgr.list_users() if u["username"] == "bob")
    assert row["role"] == "user"
    assert row["is_admin"] is False
    privs = mgr.get_privileges("bob")
    assert privs["can_manage_brain"] is False
    assert privs["can_manage_models"] is False


def test_coder_role_grants_brain_and_models(mgr):
    mgr.create_user("carol", "password123", role="coder")
    row = next(u for u in mgr.list_users() if u["username"] == "carol")
    assert row["role"] == "coder"
    assert row["is_admin"] is False
    privs = mgr.get_privileges("carol")
    assert privs["can_manage_brain"] is True
    assert privs["can_manage_models"] is True


def test_admin_role_sets_is_admin(mgr):
    mgr.create_user("alice", "password123", role="admin")
    assert mgr.is_admin("alice") is True
    row = next(u for u in mgr.list_users() if u["username"] == "alice")
    assert row["role"] == "admin"


def test_migration_assigns_role_from_is_admin(mgr):
    mgr._config["users"] = {
        "old_admin": {"password_hash": "x", "is_admin": True},
        "old_user": {"password_hash": "x", "is_admin": False},
    }
    mgr._migrate_roles()
    assert mgr.users["old_admin"]["role"] == "admin"
    assert mgr.users["old_user"]["role"] == "user"


def test_status_includes_role(mgr):
    mgr.create_user("dan", "password123", role="coder")
    tok = mgr.create_session("dan", "password123")
    st = mgr.status(tok)
    assert st["role"] == "coder"


def test_create_user_invalid_role_rejected(mgr):
    result = mgr.create_user("zed", "password123", role="superuser")
    assert result is False
    assert "zed" not in mgr.users


def test_set_role_promotes_user_to_coder(mgr):
    mgr.create_user("eve", "password123", role="user")
    mgr.create_user("root", "password123", role="admin")
    assert mgr.set_role("eve", "coder", requesting_user="root") is True
    assert mgr.get_privileges("eve")["can_manage_brain"] is True
    assert next(u for u in mgr.list_users() if u["username"] == "eve")["role"] == "coder"


def test_set_role_requires_admin_requester(mgr):
    mgr.create_user("eve", "password123", role="user")
    mgr.create_user("frank", "password123", role="coder")
    assert mgr.set_role("eve", "coder", requesting_user="frank") is False


def test_set_role_cannot_demote_last_admin(mgr):
    mgr.create_user("root", "password123", role="admin")
    assert mgr.set_role("root", "user", requesting_user="root") is False
    assert mgr.is_admin("root") is True


def test_set_role_rejects_invalid_role(mgr):
    mgr.create_user("eve", "password123", role="user")
    mgr.create_user("root", "password123", role="admin")
    assert mgr.set_role("eve", "wizard", requesting_user="root") is False


def test_set_role_demote_coder_resets_privileges(mgr):
    mgr.create_user("carol", "password123", role="coder")
    mgr.create_user("root", "password123", role="admin")
    assert mgr.set_role("carol", "user", requesting_user="root") is True
    privs = mgr.get_privileges("carol")
    assert privs["can_manage_brain"] is False
    assert privs["can_manage_models"] is False


def test_ensure_dev_accounts_creates_three_tiers(mgr):
    created = mgr.ensure_dev_accounts(password="password")
    roles = {u["username"]: u["role"] for u in mgr.list_users()}
    assert roles.get("admin") == "admin"
    assert roles.get("coder") == "coder"
    assert roles.get("user") == "user"
    assert created == 3


def test_ensure_dev_accounts_is_idempotent(mgr):
    mgr.ensure_dev_accounts(password="password")
    created2 = mgr.ensure_dev_accounts(password="password")
    assert created2 == 0


def test_create_session_for_issues_token_without_password(mgr):
    mgr.create_user("coder", "password123", role="coder")
    tok = mgr.create_session_for("coder")
    assert tok and mgr.get_username_for_token(tok) == "coder"


def test_create_session_for_unknown_user_returns_none(mgr):
    assert mgr.create_session_for("ghost") is None


def _client(tmp_path, monkeypatch, dev_switcher=True):
    monkeypatch.setenv("PRISMA_DEV_SWITCHER", "1" if dev_switcher else "0")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.auth_routes import setup_auth_routes, SESSION_COOKIE
    m = AuthManager(os.path.join(tmp_path, "auth.json"))
    m.create_user("root", "password123", role="admin")
    app = FastAPI()
    app.include_router(setup_auth_routes(m))
    # SEC-21 D-11: /switch-user now requires a direct loopback connection at
    # call time, so drive the router as one (mirrors test_dev_mint_hard_gate.py).
    c = TestClient(app, client=("127.0.0.1", 12345))
    c.post("/api/auth/login", json={"username": "root", "password": "password123"})
    return c, m, SESSION_COOKIE


def test_switch_user_requires_dev_flag(tmp_path, monkeypatch):
    c, m, _ = _client(str(tmp_path), monkeypatch, dev_switcher=False)
    m.create_user("coder", "password123", role="coder")
    r = c.post("/api/auth/switch-user", json={"username": "coder"})
    assert r.status_code == 403


def test_switch_user_swaps_session_for_admin(tmp_path, monkeypatch):
    c, m, cookie = _client(str(tmp_path), monkeypatch, dev_switcher=True)
    m.create_user("coder", "password123", role="coder")
    r = c.post("/api/auth/switch-user", json={"username": "coder"})
    assert r.status_code == 200
    st = c.get("/api/auth/status").json()
    assert st["username"] == "coder" and st["role"] == "coder"


def test_set_role_route(tmp_path, monkeypatch):
    c, m, _ = _client(str(tmp_path), monkeypatch)
    m.create_user("eve", "password123", role="user")
    r = c.put("/api/auth/users/eve/role", json={"role": "coder"})
    assert r.status_code == 200
    assert next(u for u in m.list_users() if u["username"] == "eve")["role"] == "coder"
