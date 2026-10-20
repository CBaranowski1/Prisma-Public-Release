"""SEC-31 (D-05/D-06): a crash mid-save must not corrupt api_keys.json.

Models the crash injection on tests/test_atomic_io.py's
test_atomic_write_text_preserves_target_when_replace_fails: monkeypatch
core.atomic_io.os.replace to raise, and assert the PRIOR api_keys.json
survives with the previously-saved key intact and the failed write never
lands.
"""
import pytest

import core.atomic_io as atomic_io
from src.api_key_manager import APIKeyManager


def test_crash_mid_save_preserves_prior_keys(tmp_path, monkeypatch):
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("prov_a", "secret_a")
    assert mgr.load()["prov_a"] == "secret_a"

    def boom(src, dst):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(atomic_io.os, "replace", boom)

    with pytest.raises(OSError):
        mgr.save("prov_b", "secret_b")

    # Re-load via a fresh manager instance so we're reading strictly what's
    # on disk, not anything cached in-process.
    reloaded = APIKeyManager(str(tmp_path)).load()
    assert reloaded.get("prov_a") == "secret_a", "prior key must survive a crash mid-write"
    assert "prov_b" not in reloaded, "the crashed write must not have landed"
