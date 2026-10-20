"""Optional third-party dependency detection shared across services/search/.

Both providers.py and content.py import bs4 through this module instead of
importing it directly, so a missing beautifulsoup4 install degrades the
HTML-parsing call sites (empty results / degraded content) instead of
crashing the whole services.search package at import time.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    BeautifulSoup = None  # type: ignore
    _HAS_BS4 = False
    logger.warning(
        "beautifulsoup4 not installed; HTML parsing paths in services.search will degrade to empty results"
    )


def has_bs4() -> bool:
    """Return True if beautifulsoup4 is available."""
    return _HAS_BS4
