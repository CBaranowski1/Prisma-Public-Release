"""SEC-30 (D-03): crash-safe key rotation via a write-ahead `.key.new` sidecar.

rotate_key() is the ONLY operation that touches both the key file and the
ciphertext file -- both naive write-orderings have a data-loss crash window
(RESEARCH Pitfall 2), so this test suite covers, per RESEARCH Assumption A1:

  1. clean round-trip -- all providers still decrypt after rotation, the
     canonical .key changed, and no .key.new sidecar is left behind.
  2. crash-mid-rotation -- the highest-value assertion. A crash is simulated
     between the re-encrypted api_keys.json write (step 3) and the sidecar
     promotion (step 4, os.replace). A FRESH manager instance must still
     decrypt every provider via the read-path sidecar fallback
     (MultiFernet([new, canonical])) -- proving no data is lost even though
     the canonical .key still holds the OLD key on disk.
  3. abort-on-undecryptable -- if any provider fails to decrypt with the
     current key, rotate_key() must raise and write NOTHING (no new .key,
     no .key.new, api_keys.json untouched), and the offending provider must
     be surfaced via last_decrypt_failures (D-04 mechanism).
  4. refuse-when-env-set -- PRISM_MASTER_KEY set means there is no on-disk
     key to rotate; rotate_key() must raise a clear error and write nothing.

Crash injection is modeled on tests/test_atomic_io.py:144-157
(monkeypatch os.replace to raise), but scoped to raise ONLY for the
sidecar-promotion call (a call-count-based boom) so the earlier
atomic_write_json (which itself calls os.replace internally) is allowed to
succeed and land the re-encrypted json before the simulated crash.
"""
import os

import pytest
from cryptography.fernet import Fernet

from src.api_key_manager import APIKeyManager


def test_rotate_key_clean_round_trip(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("prov_a", "secret_a")
    mgr.save("prov_b", "secret_b")
    mgr.save("prov_c", "secret_c")

    key_file = tmp_path / ".key"
    old_key_bytes = key_file.read_bytes()

    mgr.rotate_key()

    new_key_bytes = key_file.read_bytes()
    assert new_key_bytes != old_key_bytes, "rotate_key() must persist a new canonical key"

    sidecar = tmp_path / ".key.new"
    assert not sidecar.exists(), "sidecar must be cleaned up (promoted) after a successful rotation"

    reloaded = APIKeyManager(str(tmp_path)).load()
    assert reloaded == {
        "prov_a": "secret_a",
        "prov_b": "secret_b",
        "prov_c": "secret_c",
    }


def test_rotate_key_crash_between_json_write_and_promotion_still_decrypts(tmp_path, monkeypatch):
    """RESEARCH Assumption A1: the highest-scrutiny test in this phase.

    Simulate a crash strictly AFTER the re-encrypted api_keys.json has been
    written durably but BEFORE the sidecar->`.key` promotion (the final
    os.replace in rotate_key's step 4). A fresh manager instance -- which
    only ever sees what's on disk -- must still decrypt every provider by
    consulting the `.key.new` sidecar left behind by the aborted rotation.
    """
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("prov_a", "secret_a")
    mgr.save("prov_b", "secret_b")
    mgr.save("prov_c", "secret_c")

    key_file = tmp_path / ".key"
    old_key_bytes = key_file.read_bytes()

    import src.api_key_manager as akm_module

    real_replace = os.replace
    call_count = {"n": 0}

    def boom_on_promotion(src, dst):
        call_count["n"] += 1
        # The promotion call replaces the sidecar with the canonical key
        # file -- that's the LAST os.replace rotate_key makes, and it is
        # specifically `dst == self.key_file`. atomic_write_json's own
        # internal os.replace (temp -> api_keys.json) must be allowed to
        # succeed so the crash window is truly "json written, key not yet
        # promoted."
        if str(dst) == str(key_file):
            raise OSError("simulated crash between json write and key promotion")
        return real_replace(src, dst)

    monkeypatch.setattr(akm_module.os, "replace", boom_on_promotion)

    with pytest.raises(OSError):
        mgr.rotate_key()

    # Canonical .key must still hold the OLD key -- the crash happened
    # before promotion.
    assert key_file.read_bytes() == old_key_bytes

    # The sidecar must still be present -- it holds the new key that the
    # (already-written) api_keys.json was re-encrypted under.
    sidecar = tmp_path / ".key.new"
    assert sidecar.exists(), "sidecar must survive an interrupted promotion for read-path resilience"

    # A FRESH manager (simulating a new process after the crash) must still
    # decrypt every provider by trying the sidecar key via MultiFernet.
    fresh = APIKeyManager(str(tmp_path))
    reloaded = fresh.load()
    assert reloaded == {
        "prov_a": "secret_a",
        "prov_b": "secret_b",
        "prov_c": "secret_c",
    }, "read path must fall back to the .key.new sidecar so no key is lost mid-rotation"


def test_rotate_key_aborts_on_undecryptable_provider_writes_nothing(tmp_path):
    mgr = APIKeyManager(str(tmp_path))
    mgr.save("prov_good", "secret_good")

    # Seed a provider whose ciphertext cannot be decrypted with the current
    # key at all (e.g. encrypted under a foreign key / garbage token).
    foreign_key = Fernet.generate_key()
    bad_token = Fernet(foreign_key).encrypt(b"unreachable").decode()
    raw = mgr._load_raw()
    raw["prov_bad"] = bad_token
    from core.atomic_io import atomic_write_json

    atomic_write_json(mgr.api_keys_file, raw)

    key_file = tmp_path / ".key"
    old_key_bytes = key_file.read_bytes()
    old_json_bytes = (tmp_path / "api_keys.json").read_bytes()

    with pytest.raises(Exception):
        mgr.rotate_key()

    assert key_file.read_bytes() == old_key_bytes, "old key must be untouched when rotation aborts"
    assert not (tmp_path / ".key.new").exists(), "no sidecar must be left when rotation aborts"
    assert (tmp_path / "api_keys.json").read_bytes() == old_json_bytes, "api_keys.json must be untouched when rotation aborts"

    assert "prov_bad" in mgr.last_decrypt_failures


def test_rotate_key_refuses_when_prism_master_key_set(tmp_path, monkeypatch):
    valid_key = Fernet.generate_key().decode()
    monkeypatch.setenv("PRISM_MASTER_KEY", valid_key)

    mgr = APIKeyManager(str(tmp_path))
    mgr.save("prov_a", "secret_a")

    with pytest.raises(Exception):
        mgr.rotate_key()

    assert not (tmp_path / ".key").exists(), "env-managed key must never spawn an on-disk .key file"
    assert not (tmp_path / ".key.new").exists(), "env-managed key rotation must not write a sidecar"
