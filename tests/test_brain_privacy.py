"""Tests for src/brain_privacy.py — the fail-closed PII/secret gate.

Pure-function unit tests, no fixtures (mirrors tests/test_brain_learn.py's
pure-function section). All secrets below are SYNTHETIC/obviously-fake —
never a real credential. Offline; no fastembed import anywhere in this file
or in src/brain_privacy.py's dependency graph.
"""
import src.brain_privacy as bp


# ---- negatives ------------------------------------------------------------

def test_empty_string_is_clean():
    assert bp.contains_pii_or_secret("") == (False, "")


def test_none_is_clean():
    assert bp.contains_pii_or_secret(None) == (False, "")


def test_clean_prose_is_clean():
    ok, reason = bp.contains_pii_or_secret(
        "Always write settings JSON atomically to avoid corruption."
    )
    assert ok is False
    assert reason == ""


# ---- secret-shaped tokens ---------------------------------------------------

def test_openai_style_key_flagged():
    flagged, reason = bp.contains_pii_or_secret(
        "here is my key sk-FAKE1234567890ABCDEFghij for testing"
    )
    assert flagged is True
    assert reason


def test_github_pat_flagged():
    flagged, reason = bp.contains_pii_or_secret(
        "token: ghp_FAKE1234567890ABCDEFghijKLMN"
    )
    assert flagged is True
    assert reason


def test_aws_access_key_flagged():
    flagged, reason = bp.contains_pii_or_secret(
        "AKIAFAKE1234567890AB is my aws access key"
    )
    assert flagged is True
    assert reason


def test_pem_private_key_header_flagged():
    flagged, reason = bp.contains_pii_or_secret(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIFAKEFAKEFAKE==\n-----END RSA PRIVATE KEY-----"
    )
    assert flagged is True
    assert reason


def test_jwt_shaped_string_flagged():
    fake_jwt = (
        "eyJhbGciOiJIUzI1NiIsFAKEFAKEFAKE."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwFAKEFAKE."
        "SflKxwRJSMeKKFAKEsignaturepart"
    )
    flagged, reason = bp.contains_pii_or_secret(f"auth header: {fake_jwt}")
    assert flagged is True
    assert reason


# ---- structurally-regular PII ----------------------------------------------

def test_email_address_flagged():
    flagged, reason = bp.contains_pii_or_secret("reach me at jane.doe@example.com please")
    assert flagged is True
    assert "email" in reason.lower()


def test_ssn_shaped_digits_flagged():
    flagged, reason = bp.contains_pii_or_secret("my ssn is 123-45-6789 for the form")
    assert flagged is True
    assert reason


def test_credit_card_shaped_luhn_valid_flagged():
    # 4111111111111111 is the well-known Visa test number (Luhn-valid, synthetic).
    flagged, reason = bp.contains_pii_or_secret("card number 4111111111111111 on file")
    assert flagged is True
    assert "credit" in reason.lower() or "card" in reason.lower()


def test_credit_card_shaped_luhn_invalid_not_flagged_by_cc_rule():
    # Same length (16 digits) but fails Luhn -- must not trip the CC rule.
    # Also avoid accidentally matching any other pattern (no email/ssn/secret shape).
    flagged, reason = bp.contains_pii_or_secret("tracking id 1234567890123456 for the shipment")
    assert flagged is False
    assert reason == ""


def test_luhn_helper_directly():
    assert bp._luhn_ok("4111111111111111") is True
    assert bp._luhn_ok("1234567890123456") is False
