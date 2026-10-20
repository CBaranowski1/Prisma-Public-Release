"""Regression lock for SEC-24 (D-06): the dead first-user admin-fallback
resolver must never come back.

Deleted symbol: the function that used to live at companion/pairing.py and
resolved "who is admin" by parsing data/auth.json directly, falling back to
the first user in the dict if none was flagged is_admin. It had zero
production callers -- the live pairing mint path already attributes to
get_current_user(request) behind require_admin, which routes through
AuthManager. This test asserts both that the symbol is gone from the loaded
module AND that no source file under companion/ defines it again.
"""

import re
from pathlib import Path

import companion.pairing as P

_REMOVED_SYMBOL = "find_admin_user"
_DEFINITION_PATTERN = re.compile(r"^\s*def\s+find_admin_user\b", re.MULTILINE)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPANION_DIR = _REPO_ROOT / "companion"


def test_find_admin_user_symbol_absent():
    """The dead first-user-fallback resolver must not exist on the module."""
    assert not hasattr(P, _REMOVED_SYMBOL)


def test_no_definition_of_find_admin_user_under_companion():
    """No file under companion/ may (re)define the removed function.

    Matches only the `def find_admin_user` definition pattern -- not any
    prose mentioning the removed name -- so a conceptual removal note in a
    docstring/comment can't trip this lock.
    """
    offenders = []
    for path in _COMPANION_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _DEFINITION_PATTERN.search(text):
            offenders.append(str(path))
    assert offenders == [], f"def find_admin_user reintroduced in: {offenders}"
