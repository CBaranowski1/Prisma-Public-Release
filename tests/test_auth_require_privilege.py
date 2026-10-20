"""fail_closed matrix for require_privilege (SEC-04/D-07, D-07 blast-radius).

Discriminator (Pitfall 1, 14-RESEARCH.md): a test that would pass identically
with or without ``fail_closed=True`` wired in is testing the WRONG branch. An
explicit ``can_manage_data_sources=False`` already denies correctly today
(``DEFAULT_PRIVILEGES`` always stamps the key) — that is NOT the fail-open bug
and is covered elsewhere (``tests/test_query_routes_external.py``). The two
genuine fail-open branches this file targets are:
  1. the privileges dict is PRESENT but MISSING the key entirely
     (schema drift / legacy auth.json), and
  2. the privilege lookup itself raises.
Only ``fail_closed=True`` denies on those two branches; the default
(``fail_closed`` omitted / False) must keep the legacy fail-open behavior for
the 5 non-QB callers (document/gallery/memory/relationship/research routes),
locked here and in the untouched ``tests/test_auth_require_privilege_nondict.py``.
"""
import types

import pytest
from fastapi import HTTPException

from src import auth_helpers
from src.auth_helpers import require_privilege


class _Mgr:
    def __init__(self, privs):
        self._privs = privs

    def get_privileges(self, user):
        return self._privs


class _Raising:
    def get_privileges(self, user):
        raise RuntimeError("corrupt auth.json")


def _request(mgr):
    state = types.SimpleNamespace(auth_manager=mgr)
    return types.SimpleNamespace(app=types.SimpleNamespace(state=state))


def test_require_privilege_fail_closed_denies_on_missing_key(monkeypatch):
    # Dict PRESENT but WITHOUT "can_manage_data_sources" — the genuine
    # fail-open branch (Pitfall 1), not an explicit False.
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({}))
    with pytest.raises(HTTPException) as exc:
        require_privilege(req, "can_manage_data_sources", fail_closed=True)
    assert exc.value.status_code == 403


def test_require_privilege_fail_closed_denies_on_lookup_exception(monkeypatch):
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Raising())
    with pytest.raises(HTTPException) as exc:
        require_privilege(req, "can_manage_data_sources", fail_closed=True)
    assert exc.value.status_code == 403


def test_require_privilege_fail_closed_single_user_still_permitted(monkeypatch):
    # D-08: single-user / auth-disabled mode (require_user returns "") stays
    # permitted even in strict mode — the empty-user early return is
    # unchanged in both modes.
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "")
    req = _request(_Mgr({}))
    assert require_privilege(req, "can_manage_data_sources", fail_closed=True) == ""


def test_require_privilege_default_unchanged_for_other_callers(monkeypatch):
    # D-07 blast-radius lock: missing key, fail_closed omitted (defaults to
    # False) — the 5 non-QB callers keep today's fail-open behavior.
    monkeypatch.setattr(auth_helpers, "require_user", lambda request: "bob")
    req = _request(_Mgr({}))
    assert require_privilege(req, "can_use_documents") == "bob"
