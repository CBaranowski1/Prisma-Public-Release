"""Regression guards for the at-rest APIKeyManager Fernet key file
(src/api_key_manager.py::get_or_create_key).

The key must be created restrictively (0o600, never briefly world-readable)
AND an already-existing legacy world-readable key must be tightened in place
on load without ever being regenerated (that would orphan existing
ciphertext). Modeled on tests/test_secret_storage_key_perms.py.
"""
import os
import stat
import sys

import pytest

from src.api_key_manager import APIKeyManager


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_new_key_created_at_0o600(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.get_or_create_key()
    key_path = tmp_path / ".key"
    assert key_path.exists()
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600, f"key file mode is {oct(mode)}, expected 0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_existing_world_readable_key_is_tightened_and_reused(tmp_path):
    key_path = tmp_path / ".key"
    known_bytes = b"a" * 44  # not a real Fernet key, but bytes must round-trip unchanged
    key_path.write_bytes(known_bytes)
    os.chmod(key_path, 0o644)
    mode_before = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode_before == 0o644, f"fixture setup failed, mode is {oct(mode_before)}"

    mgr = APIKeyManager(str(tmp_path))
    returned = mgr.get_or_create_key()

    mode_after = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode_after == 0o600, f"key file mode is {oct(mode_after)}, expected 0o600"
    assert returned == known_bytes, "existing key bytes must be returned unchanged, never regenerated"
