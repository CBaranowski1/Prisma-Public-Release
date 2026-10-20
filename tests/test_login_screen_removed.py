from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_login_page_asset_and_route_are_removed():
    assert not (ROOT / "static" / "login.html").exists()
    app_source = _read("app.py")
    assert '@app.get("/login")' not in app_source
    assert "static/login.html" not in app_source


def test_single_user_mode_cannot_be_reenabled_by_environment():
    app_source = _read("app.py")
    assert 'os.environ["AUTH_ENABLED"] = "false"' in app_source
    assert 'os.environ["LOCALHOST_BYPASS"] = "false"' in app_source


def test_authentication_controls_are_absent_from_desktop_ui():
    html = _read("static/index.html")
    app_js = _read("static/app.js")
    blocked_html = (
        'data-settings-tab="account"',
        'data-settings-panel="account"',
        'data-settings-tab="users"',
        'data-settings-panel="users"',
        "settings-logout-btn",
        "settings-password-card",
        "settings-2fa-card",
        "data-login-as",
    )
    for marker in blocked_html:
        assert marker not in html
    assert "window.location.href = '/login'" not in app_js


def test_compose_cannot_enable_prisma_login():
    compose = _read("docker-compose.yml")
    assert "AUTH_ENABLED=false" in compose
    assert "AUTH_ENABLED=${" not in compose
    assert "PRISMA_ADMIN_PASSWORD" not in compose
