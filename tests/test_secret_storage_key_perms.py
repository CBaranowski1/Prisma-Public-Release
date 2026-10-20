"""Regression guards for the at-rest secret key file (src/secret_storage.py).

The key must be created restrictively and never silently regenerated, so a
stolen DB stays useless and an existing key is never clobbered. The atomic
0o600 creation closes a brief world-readable TOCTOU window on POSIX (the mode
is applied at create time, not after a separate chmod).
"""
import importlib
import os
import stat
import sys

import pytest


@pytest.fixture
def fresh_secret_storage(tmp_path, monkeypatch):
    """Reload secret_storage with its key path pointed at a temp dir."""
    import src.secret_storage as ss
    key_path = tmp_path / ".app_key"
    monkeypatch.setattr(ss, "_KEY_PATH", key_path, raising=True)
    monkeypatch.setattr(ss, "_fernet", None, raising=True)
    return ss, key_path


def test_encrypt_decrypt_roundtrips(fresh_secret_storage):
    ss, key_path = fresh_secret_storage
    token = ss.encrypt("hunter2-secret")
    assert token.startswith("enc:")
    assert token != "hunter2-secret"
    assert ss.decrypt(token) == "hunter2-secret"
    assert key_path.exists()


def test_existing_key_is_reused_not_regenerated(fresh_secret_storage):
    ss, key_path = fresh_secret_storage
    first = ss._load_or_create_key()
    assert key_path.exists()
    # A second load (even after clearing the cached Fernet) must read the SAME
    # key off disk, never mint a new one — otherwise old ciphertext is lost.
    ss._fernet = None
    second = ss._load_or_create_key()
    assert first == second


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode bits not meaningful on Windows")
def test_key_file_is_owner_only_on_posix(fresh_secret_storage):
    ss, key_path = fresh_secret_storage
    ss._load_or_create_key()
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600, f"key file mode is {oct(mode)}, expected 0o600"
