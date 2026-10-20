# tests/test_datasource_routes_phase3.py
"""Phase 3 routes: csv/rest creation + file upload save path + privilege gate."""
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import contextlib

import pytest
from fastapi import HTTPException

import routes.datasource_routes as mod


def _req(user="alice", *, priv_ok=True):
    privs = {"can_manage_data_sources": priv_ok}
    auth = SimpleNamespace(get_privileges=lambda u: privs, is_configured=True, is_admin=lambda u: False)
    return SimpleNamespace(state=SimpleNamespace(current_user=user),
                           app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth)),
                           client=SimpleNamespace(host="10.0.0.9"))


def _handler(method, fragment):
    for r in mod.setup_datasource_routes().routes:
        if fragment in getattr(r, "path", "") and method in (getattr(r, "methods", None) or set()):
            return r.endpoint
    raise KeyError(fragment)


@contextlib.contextmanager
def _db_ctx(s): yield s


def test_create_rest_source_persists_options(monkeypatch):
    captured = {}

    class _DS:
        def __init__(self, **kw): captured.update(kw); self.__dict__.update(kw)
        def to_dict(self): return {"id": captured.get("id"), "source_type": captured.get("source_type")}

    monkeypatch.setattr(mod, "DataSource", _DS)
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(MagicMock()))
    create = _handler("POST", "/datasources")
    body = mod.DataSourceCreate(name="API", source_type="rest",
                                options={"base_url": "https://x.test", "endpoints": {}})
    resp = create(request=_req(), body=body)
    assert resp["source_type"] == "rest"
    assert json.loads(captured["options"])["base_url"] == "https://x.test"


def test_create_rejects_unknown_type(monkeypatch):
    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(MagicMock()))
    create = _handler("POST", "/datasources")
    body = mod.DataSourceCreate(name="x", source_type="oracle")
    with pytest.raises(HTTPException) as exc:
        create(request=_req(), body=body)
    assert exc.value.status_code == 400
