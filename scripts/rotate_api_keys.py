"""Rotate the on-disk encryption key for stored API keys (SEC-30/D-03).

This is an operator action, never a request path -- it is not exposed via
any route. It decrypts every stored provider's API key with the CURRENT
key, generates a fresh key, and re-encrypts everything under it using the
crash-safe write-ahead sidecar sequence in
src.api_key_manager.APIKeyManager.rotate_key().

Usage:
    python scripts/rotate_api_keys.py

Exits non-zero (printing the raised error) if rotation cannot proceed --
e.g. PRISM_MASTER_KEY is set (no on-disk key to rotate), or a stored
provider fails to decrypt with the current key (rotate_key() aborts rather
than risk permanently losing that entry).
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constants import DATA_DIR  # noqa: E402
from src.api_key_manager import APIKeyManager  # noqa: E402


def main() -> int:
    manager = APIKeyManager(DATA_DIR)

    before = manager.load()
    before_count = len(before)
    print(f"Providers before rotation: {before_count}")

    try:
        manager.rotate_key()
    except Exception as e:  # noqa: BLE001 - surface any rotation failure to the operator
        print(f"Rotation FAILED: {e}", file=sys.stderr)
        if manager.last_decrypt_failures:
            print(
                f"Providers that failed to decrypt with the current key: "
                f"{', '.join(manager.last_decrypt_failures)}",
                file=sys.stderr,
            )
        return 1

    after = manager.load()
    after_count = len(after)
    print(f"Providers after rotation: {after_count}")
    if manager.last_decrypt_failures:
        print(f"Decrypt failures after rotation: {', '.join(manager.last_decrypt_failures)}")
    print("Rotation complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
