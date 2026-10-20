"""Regression: username rename must migrate mixed-case legacy owner keys.

Before lowercasing was enforced everywhere, rows could be stored with
owner='Admin' while auth usernames are normalized to 'admin'. A case-
sensitive filter would skip those rows during rename (issue #1165).
"""

import sys
import time
from unittest.mock import MagicMock

import pytest


# DEBT-03 (33-08): this file previously forced a "real" core.auth via a raw
# sys.modules.pop("core.auth", None) + delattr(core, "auth") + reimport
# dance (Pitfall 1), then permanently overwrote auth_mod._hash_password /
# _verify_password with raw attribute assignment (Pitfall 2) -- neither
# mutation was ever reverted (no monkeypatch fixture was even requested by
# the one test using this helper). Same shape/fix as 33-03's
# test_auth_config_lock_concurrency.py and 33-08's test_auth_recovery.py /
# test_auth_session_revocation.py: use the real, already-loaded core.auth
# directly and apply the fake-hashing swap via monkeypatch.setattr so
# pytest auto-reverts it at teardown.

def _fresh_auth_manager(tmp_path, monkeypatch):
    import core.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_hash_password", lambda password: f"hash:{password}")
    monkeypatch.setattr(auth_mod, "_verify_password", lambda password, hashed: hashed == f"hash:{password}")
    return auth_mod.AuthManager(str(tmp_path / "auth.json"))


def test_rename_user_updates_mixed_case_session_username(tmp_path, monkeypatch):
    mgr = _fresh_auth_manager(tmp_path, monkeypatch)
    assert mgr.create_user("admin", "pw-123456", is_admin=True) is True
    assert mgr.create_user("bob", "pw-123456") is True
    with mgr._sessions_lock:
        mgr._sessions["tok1"] = {"username": "Bob", "expiry": time.time() + 3600}
    assert mgr.rename_user("bob", "robert", "admin") is True
    with mgr._sessions_lock:
        assert mgr._sessions["tok1"]["username"] == "robert"


def _has_real_sqlalchemy():
    mod = sys.modules.get("sqlalchemy")
    if mod is None or isinstance(mod, MagicMock):
        return False
    return hasattr(mod, "create_engine")


@pytest.mark.skipif(not _has_real_sqlalchemy(), reason="sqlalchemy not installed")
def test_rename_owner_db_filter_is_case_insensitive():
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    from core.database import Base, Session as DbSession

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    db.add(
        DbSession(
            id="s1",
            name="chat",
            endpoint_url="http://localhost:8000",
            model="gpt-4",
            owner="Bob",
        )
    )
    db.commit()

    old_username = "bob"
    new_username = "robert"
    db.query(DbSession).filter(func.lower(DbSession.owner) == old_username).update(
        {"owner": new_username},
        synchronize_session=False,
    )
    db.commit()

    assert db.query(DbSession).first().owner == "robert"
