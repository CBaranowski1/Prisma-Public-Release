"""Search package — drop-in replacement for the monolithic search_engine module."""

import importlib

from .core import (
    comprehensive_web_search,
    get_search_config,
    invalidate_search_cache,
    searxng_search_results,
    update_search_config,
)
from .content import fetch_webpage_content
from .providers import searxng_search, searxng_search_api, PROVIDER_INFO
from .analytics import get_search_stats, SearchEngineError, NetworkError, ParseError, RateLimitError

# Every src.search.<name> submodule below is a sys.modules alias of the
# canonical services.search.<name> implementation (see each submodule's
# docstring). This audit proves the alias held at call time — if a future
# change reverts one of them to a by-reference re-export (or anything else
# that leaves the two module objects distinct), this raises loudly instead
# of letting the shim silently drift from the canonical implementation.
_ALIASED_SEARCH_SHIMS = (
    "analytics",
    "cache",
    "content",
    "core",
    "providers",
    "query",
    "ranking",
)


def audit_search_shim_identity() -> None:
    """Verify every src.search.* compat shim is still sys.modules-aliased.

    Raises RuntimeError naming the offending submodule if any shim is not the
    exact same object as its services.search.* canonical counterpart.
    """
    for name in _ALIASED_SEARCH_SHIMS:
        shim = importlib.import_module(f"src.search.{name}")
        canonical = importlib.import_module(f"services.search.{name}")
        if shim is not canonical:
            raise RuntimeError(
                f"src.search.{name} is no longer aliased to services.search.{name} "
                "(sys.modules identity broken) — the compat shim has drifted "
                "from the canonical implementation."
            )


__all__ = [
    "audit_search_shim_identity",
    "comprehensive_web_search",
    "fetch_webpage_content",
    "get_search_config",
    "get_search_stats",
    "invalidate_search_cache",
    "searxng_search",
    "searxng_search_api",
    "searxng_search_results",
    "update_search_config",
    "PROVIDER_INFO",
    "SearchEngineError",
    "NetworkError",
    "ParseError",
    "RateLimitError",
]
