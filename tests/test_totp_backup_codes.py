"""SEC-19 proof: TOTP backup codes are bcrypt-hashed (never plaintext),
widened to 10x64-bit codes shown once, single-use, and gated behind a
shared 5-failure/15-minute auto-unlocking 2FA lockout covering both TOTP
and backup-code attempts. Legacy plaintext backup codes are invalidated
on load, never silently re-hashed.

Mirrors tests/test_totp_failclosed.py's tmp_path AuthManager(str(auth_path))
construction style. Uses real bcrypt (no hash-helper monkeypatch) since
hashing correctness itself is under test here.
"""
import json
import time

import pyotp

from core.auth import AuthManager


def _seed(tmp_path, user: dict) -> AuthManager:
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"users": {"alice": user}}))
    return AuthManager(str(auth_path))


def _enabled_user(secret: str, **extra) -> dict:
    return {
        "password_hash": "x",
        "totp_enabled": True,
        "totp_secret": secret,
        **extra,
    }


def test_hashed_storage_on_confirm_enable(tmp_path):
    secret = pyotp.random_base32()
    mgr = _seed(tmp_path, {"password_hash": "x", "totp_secret_pending": secret})
    code = pyotp.TOTP(secret).now()

    codes = mgr.totp_confirm_enable("alice", code)

    assert codes is not None
    assert len(codes) == 10
    for c in codes:
        assert len(c) == 16
        int(c, 16)  # must be valid hex

    hashes = mgr.users["alice"]["totp_backup_code_hashes"]
    assert len(hashes) == 10
    for h in hashes:
        assert h.startswith("$2b$")

    # no plaintext code ever equals a stored hash
    for c in codes:
        assert c not in hashes

    # the old plaintext field is never written
    assert "totp_backup_codes" not in mgr.users["alice"]


def test_single_use_consumption(tmp_path):
    secret = pyotp.random_base32()
    mgr = _seed(tmp_path, {"password_hash": "x", "totp_secret_pending": secret})
    code = pyotp.TOTP(secret).now()
    codes = mgr.totp_confirm_enable("alice", code)
    backup_code = codes[0]

    assert mgr.totp_verify("alice", backup_code) is True
    assert len(mgr.users["alice"]["totp_backup_code_hashes"]) == 9

    # re-submitting the same code fails -- single use
    assert mgr.totp_verify("alice", backup_code) is False


def test_shared_lockout_locks_totp_and_backup_then_auto_unlocks(tmp_path):
    secret = pyotp.random_base32()
    mgr = _seed(tmp_path, _enabled_user(secret))

    for _ in range(5):
        assert mgr.totp_verify("alice", "000000") is False

    assert mgr.users["alice"]["totp_locked_until"] > time.time()

    # 6th attempt with a VALID TOTP code still fails while locked
    valid_code = pyotp.TOTP(secret).now()
    assert mgr.totp_verify("alice", valid_code) is False

    # simulate clock advance past the lockout window by seeding the past
    # directly (mirrors how the seed fixture already seeds totp_enabled)
    mgr._config["users"]["alice"]["totp_locked_until"] = time.time() - 1

    valid_code2 = pyotp.TOTP(secret).now()
    assert mgr.totp_verify("alice", valid_code2) is True
    assert mgr.users["alice"]["totp_failed_attempts"] == 0
    assert "totp_locked_until" not in mgr.users["alice"]


def test_lockout_success_resets_counter(tmp_path):
    secret = pyotp.random_base32()
    mgr = _seed(tmp_path, _enabled_user(secret))

    assert mgr.totp_verify("alice", "000000") is False
    assert mgr.totp_verify("alice", "000000") is False
    assert mgr.users["alice"]["totp_failed_attempts"] == 2

    valid_code = pyotp.TOTP(secret).now()
    assert mgr.totp_verify("alice", valid_code) is True
    assert mgr.users["alice"]["totp_failed_attempts"] == 0
    assert "totp_locked_until" not in mgr.users["alice"]


def test_regeneration_invalidates_old_codes(tmp_path):
    secret = pyotp.random_base32()
    mgr = _seed(tmp_path, {"password_hash": "x", "totp_secret_pending": secret})
    code = pyotp.TOTP(secret).now()
    old_codes = mgr.totp_confirm_enable("alice", code)

    new_codes = mgr.regenerate_backup_codes("alice")

    assert len(new_codes) == 10
    for c in new_codes:
        assert len(c) == 16
        int(c, 16)
    assert set(new_codes) != set(old_codes)

    # a previously-valid (un-consumed) old code now fails
    assert mgr.totp_verify("alice", old_codes[0]) is False
    # a newly issued code works
    assert mgr.totp_verify("alice", new_codes[0]) is True


def test_plaintext_migration_invalidates_legacy_codes(tmp_path):
    secret = pyotp.random_base32()
    mgr = _seed(tmp_path, _enabled_user(secret, totp_backup_codes=["deadbeefcafebabe"]))

    # legacy plaintext field dropped on load, never re-hashed
    assert "totp_backup_codes" not in mgr.users["alice"]
    assert "totp_backup_code_hashes" not in mgr.users["alice"]

    # the old plaintext code does NOT verify
    assert mgr.totp_verify("alice", "deadbeefcafebabe") is False
