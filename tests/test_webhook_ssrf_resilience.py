import sys
import json
from datetime import datetime

# conftest.py stubs src.database with a fake module; webhook_manager imports
# from it, so temporarily evict the stub for THIS file's own import of
# src.webhook_manager, then restore whatever was there before -- both the
# sys.modules[fullname] entry AND the parent package's own __dict__
# attribute (sys.modules["src"].database), which the import system sets
# during a genuine fresh import but a plain `sys.modules[key] = value`
# restore does NOT touch.
#
# DEBT-03 (33-08): this used to `del sys.modules["src.database"]` (and
# sometimes "core.database") permanently, with no restore -- and the FIRST
# fix attempt here only restored the sys.modules dict entry, which turned
# out to be Pitfall 1 all over again: `from src.webhook_manager import ...`
# triggers a fresh `src.database` import (since the stub was evicted),
# and Python's import machinery sets BOTH sys.modules["src.database"] AND
# sys.modules["src"].__dict__["database"] to the new real module object.
# Restoring only the sys.modules dict entry left the parent package's own
# attribute permanently pointing at that new real module -- so
# `import src.database as x` (attribute-style resolution via the parent
# package) diverged from `from src.database import Y` (sys.modules-dict-
# style resolution) for the rest of the session, breaking
# tests/test_openai_facade_integration.py's `integration_client` fixture
# (which does `import src.database as src_db` then monkeypatches `src_db`
# -- silently patching the wrong, orphaned module object). Restoring BOTH
# halves in lockstep keeps every resolution style consistent.
def _save_module(name):
    """Return (dict_entry, parent_pkg, had_attr, attr_value) for `name`."""
    dict_entry = sys.modules.get(name)
    parent_name, _, child_name = name.rpartition(".")
    parent_pkg = sys.modules.get(parent_name) if parent_name else None
    had_attr = parent_pkg is not None and hasattr(parent_pkg, child_name)
    attr_value = getattr(parent_pkg, child_name, None) if had_attr else None
    return dict_entry, parent_pkg, had_attr, attr_value


def _restore_module(name, saved):
    dict_entry, parent_pkg, had_attr, attr_value = saved
    if dict_entry is not None:
        sys.modules[name] = dict_entry
    else:
        sys.modules.pop(name, None)
    if parent_pkg is not None:
        child_name = name.rpartition(".")[2]
        if had_attr:
            setattr(parent_pkg, child_name, attr_value)
        elif hasattr(parent_pkg, child_name):
            delattr(parent_pkg, child_name)


_saved_src_database = _save_module("src.database")
sys.modules.pop("src.database", None)

_core_database = sys.modules.get("core.database")
_core_database_all = getattr(_core_database, "__all__", None) if _core_database is not None else None
_evict_core_database = (
    _core_database is not None
    and (
        not getattr(_core_database, "__file__", None)
        or (
            _core_database_all is not None
            and (
                not isinstance(_core_database_all, (list, tuple, set))
                or not all(isinstance(name, str) for name in _core_database_all)
            )
        )
    )
)
if _evict_core_database:
    _saved_core_database = _save_module("core.database")
    sys.modules.pop("core.database", None)

import pytest
from src.webhook_manager import validate_webhook_url

_restore_module("src.database", _saved_src_database)
if _evict_core_database:
    _restore_module("core.database", _saved_core_database)


def test_webhook_url_ssrf_mitigation():
    # SSRF bypasses that must be rejected, including IPv6 unspecified and
    # IPv4-mapped IPv6 (loopback + cloud metadata).
    private_urls = [
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://127.0.0.1/",
        "http://0.0.0.0/",
    ]
    for url in private_urls:
        with pytest.raises(ValueError) as exc:
            validate_webhook_url(url)
        assert "private/internal addresses" in str(exc.value)

    # A clearly public IP literal must still be accepted.
    public_url = "http://93.184.216.34/"
    assert validate_webhook_url(public_url) == public_url


@pytest.mark.asyncio
async def test_webhook_delivery_uses_naive_utc_timestamps(monkeypatch):
    import src.webhook_manager as wm

    class _Query:
        def __init__(self, updates):
            self.updates = updates

        def filter(self, *_args, **_kwargs):
            return self

        def update(self, values):
            self.updates.append(values)

    class _Db:
        def __init__(self):
            self.updates = []
            self.committed = False
            self.closed = False

        def query(self, _model):
            return _Query(self.updates)

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            self.closed = True

    class _Response:
        status_code = 204

    class _Client:
        def __init__(self):
            self.content = ""

        async def post(self, _url, content, headers):
            self.content = content
            assert headers["X-Prisma-Event"] == "webhook.test"
            return _Response()

    db = _Db()
    client = _Client()
    monkeypatch.setattr(wm, "SessionLocal", lambda: db)

    manager = wm.WebhookManager()
    await manager._client.aclose()
    manager._client = client

    await manager._deliver("hook-1", "http://93.184.216.34/", None, "webhook.test", {"ok": True})

    body = json.loads(client.content)
    payload_timestamp = datetime.fromisoformat(body["timestamp"])
    assert payload_timestamp.tzinfo is None
    assert db.updates[0]["last_triggered_at"].tzinfo is None
    assert db.updates[0]["last_status_code"] == 204
    assert db.committed is True
    assert db.closed is True
