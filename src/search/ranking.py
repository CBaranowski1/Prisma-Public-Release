"""Compatibility wrapper for the canonical services.search.ranking module.

``src.search.ranking`` remains importable for older agent/deep-research code
(exposing ``rank_search_results``/``recency_score``), but the implementation
now lives in ``services.search.ranking`` so the two cannot drift out of sync
again. This used to be a by-reference re-export, which binds names at import
time and can go stale if the canonical functions are ever reassigned; full
sys.modules aliasing makes the two module objects identical instead.
"""

import sys

from services.search import ranking as _ranking

sys.modules[__name__] = _ranking
