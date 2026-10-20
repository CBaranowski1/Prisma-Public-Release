"""Prisma Desktop stays in its explicit single-user, no-login mode."""
import os

import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    from fastapi.testclient import TestClient
    import app as appmod
    return TestClient(appmod.app)


def test_stale_auth_enabled_setting_cannot_restore_login_gate(client):
    # app.py deliberately overrides old AUTH_ENABLED=true deployment files.
    r = client.get("/api/models")
    assert r.status_code == 200, r.text


def test_health_stays_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200
