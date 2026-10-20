"""SEC-30 (D-04): decrypt failures in APIKeyManager.load() must be SURFACED,
not silently dropped at warning level.

load()'s RETURN VALUE and no-raise resilience are byte-identical to before
(Pitfall 4) -- this test only adds coverage for the NEW surfaced-failure
side-channel (self.last_decrypt_failures + ERROR-level log). It does not
touch tests/test_api_key_manager_resilience.py or
tests/test_api_key_manager_corrupt_load.py, which must stay green unmodified.
"""
import json
import logging

from src.api_key_manager import APIKeyManager


def test_decrypt_failure_is_surfaced(tmp_path, caplog):
    mgr = APIKeyManager(str(tmp_path))

    # Seed one good, decryptable entry via the normal save() path.
    mgr.save("good", "good_value")

    # Hand-write a second, deliberately-undecryptable entry directly into
    # api_keys.json (bypassing save(), which would encrypt it correctly).
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    keys["bad_provider"] = "not-a-fernet-token"
    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)

    with caplog.at_level(logging.ERROR, logger="src.api_key_manager"):
        loaded = mgr.load()

    # 1) return value unchanged: only the good, decryptable subset.
    assert loaded == {"good": "good_value"}

    # 2) load() must not raise (already implied by reaching this line).

    # 3) failed provider name(s) retained on the manager.
    assert mgr.last_decrypt_failures == ["bad_provider"]

    # 4) the failure is logged at ERROR (not WARNING) from this module's logger.
    error_records = [
        r for r in caplog.records
        if r.name == "src.api_key_manager" and r.levelno == logging.ERROR
    ]
    assert len(error_records) == 1
    assert "bad_provider" in error_records[0].getMessage()


def test_clean_load_resets_last_decrypt_failures(tmp_path):
    mgr = APIKeyManager(str(tmp_path))

    # First, a load with a failure populates last_decrypt_failures.
    mgr.save("good", "good_value")
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    keys["bad_provider"] = "not-a-fernet-token"
    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)
    mgr.load()
    assert mgr.last_decrypt_failures == ["bad_provider"]

    # A subsequent all-good load must reset it back to [].
    mgr.save("good", "good_value")
    with open(mgr.api_keys_file, "r", encoding="utf-8") as f:
        keys = json.load(f)
    del keys["bad_provider"]
    with open(mgr.api_keys_file, "w", encoding="utf-8") as f:
        json.dump(keys, f)

    loaded = mgr.load()
    assert loaded == {"good": "good_value"}
    assert mgr.last_decrypt_failures == []
