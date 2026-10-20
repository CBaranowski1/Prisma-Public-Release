import os, pathlib, sys
from unittest.mock import MagicMock
import pytest

# Env this module needs — set by the fixture below and RESTORED afterwards.
# (A bare module-level os.environ[...] = ... leaked AUTH_ENABLED=false etc.
# into every test that ran after this file in the same pytest process.)
_BRAIN_ENV = {
    "AUTH_ENABLED": "false",
    "LOCALHOST_BYPASS": "true",
    "PRISMA_BRAIN_VAULT": str(pathlib.Path("tests/fixtures/vault").resolve()),
    # Point the modular-brains root at a non-existent dir so the only brain the
    # app sees is the legacy bridge (the fixtures vault above) -> deterministic,
    # and it won't pick up the real brains/ folder (the precision-audit brain).
    "PRISMA_BRAINS_ROOT": str(pathlib.Path("tests/fixtures/_no_brains").resolve()),
}


@pytest.fixture(autouse=True, scope="module")
def _brain_env():
    saved = {k: os.environ.get(k) for k in _BRAIN_ENV}
    os.environ.update(_BRAIN_ENV)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture(autouse=True, scope="module")
def _isolate_settings(tmp_path_factory):
    """Redirect the settings file to a temp path for this module so the config
    PUT/GET roundtrip (and an interrupted run) can never clobber the real
    data/settings.json (e.g. a user's configured brain_vault_path). save/load/
    get_setting all reference the module global SETTINGS_FILE at call time."""
    import src.settings as settings_mod
    orig = settings_mod.SETTINGS_FILE
    settings_mod.SETTINGS_FILE = str(tmp_path_factory.mktemp("brain_cfg") / "settings.json")
    try:
        yield
    finally:
        settings_mod.SETTINGS_FILE = orig


@pytest.fixture(scope="module")
def client(_brain_env, _isolate_settings):
    # Import app INSIDE the fixture (run time), not at collection time: sibling
    # test modules temporarily stub core.database / src.request_models etc. at
    # their own collection; importing app during collection can race that and
    # fail route registration. By run time those stubs are restored.
    # conftest stubs src.database with only SessionLocal + ModelEndpoint;
    # app.py's import chain (src.webhook_manager) also needs Webhook.
    _db_stub = sys.modules.get("src.database")
    if _db_stub is not None and not hasattr(_db_stub, "Webhook"):
        _db_stub.Webhook = MagicMock()
    _heal_polluted_modules()
    from fastapi.testclient import TestClient
    import app as app_module
    return TestClient(app_module.app)


def _heal_polluted_modules():
    """Sibling unit tests stub model modules (src.request_models, core.models)
    with MagicMock and import route modules against them, leaving routes.* bound
    to mocks in sys.modules. That makes app route-registration raise FastAPIError
    when we build the full app. Drop mock-contaminated model modules, then force
    app + all routes.* to re-import so they rebind against the real models.
    Only models that are ACTUALLY mocks are dropped (don't re-import a real ORM
    module — SQLAlchemy would re-declare tables)."""
    for name in ("src.request_models", "core.models", "core.session_manager"):
        mod = sys.modules.get(name)
        if mod is not None and type(mod).__name__ == "MagicMock":
            sys.modules.pop(name, None)
    # Force the app + route modules to rebuild against whatever models resolve
    # now (real). Safe: these are app/route definition modules, not ORM tables.
    for name in [n for n in list(sys.modules) if n == "app" or n.startswith("routes.")]:
        sys.modules.pop(name, None)


def test_providers_listed(client):
    r = client.get("/api/brain/providers"); assert r.status_code == 200, r.text
    ids = [p["provider_id"] for p in r.json()]
    # The combined provider + the legacy-bridge brain (the fixtures vault).
    assert "brain:all" in ids
    assert "brain:legacy" in ids

def test_search_endpoint(client):
    r = client.get("/api/brain/search", params={"q": "partition retention", "provider": "brain:all"})
    assert r.status_code == 200, r.text
    assert r.json() and r.json()[0]["title"] == "Postgres Partitioning"

def test_categories_endpoint(client):
    r = client.get("/api/brain/categories", params={"provider": "brain:all"})
    assert r.status_code == 200, r.text
    assert "postgres" in r.json()

def test_config_get_returns_vault_path_key(client):
    r = client.get("/api/brain/config")
    assert r.status_code == 200, r.text
    assert "vault_path" in r.json()

def test_config_put_and_get_roundtrip(client):
    r0 = client.get("/api/brain/config")
    original_path = r0.json().get("vault_path", "") if r0.status_code == 200 else ""

    test_path = "/tmp/test-brain-vault"
    r = client.put("/api/brain/config", json={"vault_path": test_path})
    assert r.status_code == 200, r.text
    assert r.json()["vault_path"] == test_path
    r2 = client.get("/api/brain/config")
    assert r2.status_code == 200, r2.text
    assert r2.json()["vault_path"] == test_path

    # Restore so this test doesn't pollute others.
    client.put("/api/brain/config", json={"vault_path": original_path})
