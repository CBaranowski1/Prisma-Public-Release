"""Regression guards for the optional PRISM_MASTER_KEY env override
(src/api_key_manager.py::get_or_create_key, SEC-30/D-02).

When PRISM_MASTER_KEY is set to a valid Fernet key, it must be used in
place of the on-disk .key file entirely -- so a read of data/ alone
yields ciphertext but never the key. When set but invalid, the manager
must FAIL CLOSED (raise) rather than silently generating a new key and
orphaning existing ciphertext. When unset, the plan-01 on-disk 0o600 key
path is unchanged (backward-compatible).
"""
import os

import pytest
from cryptography.fernet import Fernet

from src.api_key_manager import APIKeyManager


def test_env_master_key_valid_round_trips_and_writes_no_key_file(tmp_path, monkeypatch):
    valid_key = Fernet.generate_key().decode()
    monkeypatch.setenv("PRISM_MASTER_KEY", valid_key)

    mgr = APIKeyManager(str(tmp_path))
    encrypted = mgr.encrypt_api_key("hunter2-secret")
    assert encrypted != "hunter2-secret"
    assert mgr.decrypt_api_key(encrypted) == "hunter2-secret"

    key_file = tmp_path / ".key"
    assert not os.path.exists(key_file), "env master key must never be persisted to disk"


def test_env_master_key_invalid_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_MASTER_KEY", "not-a-valid-key")

    mgr = APIKeyManager(str(tmp_path))
    with pytest.raises(RuntimeError):
        mgr.get_or_create_key()

    key_file = tmp_path / ".key"
    assert not os.path.exists(key_file), "invalid env key must never generate/write a new key"


def test_env_master_key_unset_falls_back_to_on_disk_key(tmp_path, monkeypatch):
    monkeypatch.delenv("PRISM_MASTER_KEY", raising=False)

    mgr = APIKeyManager(str(tmp_path))
    encrypted = mgr.encrypt_api_key("hunter2-secret")
    assert mgr.decrypt_api_key(encrypted) == "hunter2-secret"

    key_file = tmp_path / ".key"
    assert os.path.exists(key_file), "on-disk key must be created when env var is unset"
