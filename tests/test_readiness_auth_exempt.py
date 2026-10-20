"""CR-01 regression: the Docker HEALTHCHECK curls GET /api/ready with no
credentials. With AUTH_ENABLED=true and LOCALHOST_BYPASS unset/false, that
request flows through AuthMiddleware — so /api/ready MUST be auth-exempt or the
container is marked permanently unhealthy (401 -> curl -f fails -> restart loop).

This exercises the real MIDDLEWARE path via TestClient (not check_readiness()
directly), which is the exact surface the phase's other tests did not cover.
"""
import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Fresh data dir + auth on + no loopback bypass == the hardened deploy
    # config the checklist mandates and the config the HEALTHCHECK runs under.
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    from fastapi.testclient import TestClient
    import app as appmod
    return TestClient(appmod.app)


def test_ready_is_auth_exempt_through_middleware(client):
    # No cookie, no bearer token — exactly what the HEALTHCHECK curl sends.
    # Before the fix /api/ready was not in AUTH_EXEMPT_EXACT, so the middleware
    # returned 401 ("Setup required"/"Not authenticated"); the container could
    # never go healthy. After the fix it must return the readiness JSON (200).
    r = client.get("/api/ready")
    assert r.status_code != 401, f"readiness endpoint is auth-gated: {r.text}"
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("ready"), bool)
    assert "checks" in body


def test_protected_endpoint_still_rejected(client):
    # Guard against over-exemption: a normal API route stays 401 without creds.
    r = client.get("/api/models")
    assert r.status_code == 401, r.text
