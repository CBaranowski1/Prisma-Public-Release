"""Phase 19 SC (D-07): assembled, on-machine integration proof.

This is the phase-level integration-checker connection ROADMAP SC-3
requires: it constructs the REAL `APIKeyManager` exactly the way
`src/app_initializer.py:53` does (`APIKeyManager(DATA_DIR)` -- a single
positional data-dir argument, here `tmp_path`) and proves all four D-07
behaviors together on that assembled object, not on isolated units:

  (a) a simulated crash mid-write (monkeypatch os.replace to raise) leaves
      the PRIOR api_keys.json fully intact -- no wipe (SEC-31).
  (b) a decrypt failure is SURFACED (ERROR log + last_decrypt_failures
      populated), not silently dropped (SEC-30/D-04).
  (c) the .key file is mode 0o600 (POSIX; Windows-skip) (SEC-30/D-01).
  (d) a rotate_key() round-trip re-encrypts all providers and they still
      decrypt afterward, with no stale .key.new sidecar (SEC-30/D-03).

Each behavior is its own test function so a failure pinpoints exactly which
SC behavior regressed. This file does not re-test edge cases already
covered by tests/test_api_key_manager_{key_perms,decrypt_surface,rotation,
atomic_save,concurrent_save,corrupt_load,resilience,master_key}.py -- those
stay the unit-level suite; this is the assembled proof.
"""
import json
import logging
import os
import stat
import sys

import pytest

from src.api_key_manager import APIKeyManager


def test_sc_crash_mid_write_preserves_prior_keys(tmp_path, monkeypatch):
    """D-07(a) / SEC-31: a simulated crash mid-write must not wipe prior keys."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("prov_a", "secret_a")
    mgr.save("prov_b", "secret_b")

    before_load = mgr.load()
    assert before_load == {"prov_a": "secret_a", "prov_b": "secret_b"}
    raw_bytes_before = (tmp_path / "api_keys.json").read_bytes()

    import core.atomic_io as atomic_io_module

    def boom(src, dst):
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(atomic_io_module.os, "replace", boom)

    with pytest.raises(OSError):
        mgr.save("prov_c", "secret_c")

    # The prior api_keys.json must be byte-identical -- no wipe, no partial
    # truncation (atomic_write_json writes to a sibling tmp file first).
    assert (tmp_path / "api_keys.json").read_bytes() == raw_bytes_before

    # A fresh manager instance (simulating a new process after the crash)
    # must still decrypt exactly the two prior providers -- prov_c never
    # landed.
    fresh = APIKeyManager(str(tmp_path))
    reloaded = fresh.load()
    assert reloaded == {"prov_a": "secret_a", "prov_b": "secret_b"}


def test_sc_decrypt_failure_is_surfaced(tmp_path, caplog):
    """D-07(b) / SEC-30(D-04): a decrypt failure must be surfaced, not dropped."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("good", "good_value")

    # Hand-write an undecryptable entry directly into api_keys.json,
    # bypassing save() (which would encrypt it correctly).
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    keys["bad_provider"] = "not-a-fernet-token"
    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)

    with caplog.at_level(logging.ERROR, logger="src.api_key_manager"):
        loaded = mgr.load()

    # load() must not raise (already implied by reaching this line) and must
    # return the good subset only -- never a silently-partial dict presented
    # as complete.
    assert loaded == {"good": "good_value"}

    # The failed provider name must be retained for operator visibility.
    assert mgr.last_decrypt_failures == ["bad_provider"]

    # The failure must be logged at ERROR (not WARNING).
    error_records = [
        r for r in caplog.records
        if r.name == "src.api_key_manager" and r.levelno == logging.ERROR
    ]
    assert len(error_records) == 1
    assert "bad_provider" in error_records[0].getMessage()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_sc_key_file_is_mode_0600(tmp_path):
    """D-07(c) / SEC-30(D-01): the .key file must be 0o600 on POSIX."""
    mgr = APIKeyManager(str(tmp_path))
    mgr.get_or_create_key()

    key_path = tmp_path / ".key"
    assert key_path.exists()
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600, f"key file mode is {oct(mode)}, expected 0o600"


def test_sc_rotate_key_round_trip_all_providers_still_decrypt(tmp_path):
    """D-07(d) / SEC-30(D-03): rotate_key() re-encrypts everything and all
    providers still decrypt afterward, with no stale sidecar left behind."""
    mgr = APIKeyManager(str(tmp_path))
    providers = {
        "prov_a": "secret_a",
        "prov_b": "secret_b",
        "prov_c": "secret_c",
    }
    for provider, secret in providers.items():
        mgr.save(provider, secret)

    key_file = tmp_path / ".key"
    old_key_bytes = key_file.read_bytes()

    mgr.rotate_key()

    new_key_bytes = key_file.read_bytes()
    assert new_key_bytes != old_key_bytes, "rotate_key() must persist a new canonical key"

    sidecar = tmp_path / ".key.new"
    assert not sidecar.exists(), "no stale .key.new sidecar must remain after a clean rotation"

    # A fresh manager instance -- simulating a new process reading only what
    # is on disk -- must still decrypt every provider under the new key.
    fresh = APIKeyManager(str(tmp_path))
    reloaded = fresh.load()
    assert reloaded == providers
    assert fresh.last_decrypt_failures == []
