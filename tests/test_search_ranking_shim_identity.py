"""Identity tests for the src.search.* compat shims and their startup audit.

src.search.ranking used to be a by-reference `from services.search.ranking
import (...)` re-export, which binds names at import time — if the canonical
module's functions are ever reassigned (monkeypatch, hot-reload, refactor),
the shim keeps pointing at the stale objects. Full sys.modules aliasing (the
pattern already used by core/providers/analytics/cache/content/query) makes
the shim and the canonical module the exact same object, so there is nothing
left to drift. These tests encode that target state.
"""

import sys
from types import ModuleType


def test_ranking_shim_is_aliased():
    import src.search.ranking
    import services.search.ranking

    assert sys.modules["src.search.ranking"] is sys.modules["services.search.ranking"]
    assert src.search.ranking is services.search.ranking


def test_audit_raises_on_drift():
    from src.search import audit_search_shim_identity

    # Correctly aliased: no raise.
    audit_search_shim_identity()

    # Simulate drift: replace the ranking shim's sys.modules entry with a
    # throwaway module object that is NOT the canonical services.search.ranking.
    real_module = sys.modules["src.search.ranking"]
    fake_module = ModuleType("src.search.ranking")
    sys.modules["src.search.ranking"] = fake_module
    try:
        try:
            audit_search_shim_identity()
        except RuntimeError as e:
            assert "ranking" in str(e)
        else:
            raise AssertionError("audit_search_shim_identity() did not raise on drift")
    finally:
        sys.modules["src.search.ranking"] = real_module
