# services/youtube/youtube_handler.py
"""Compatibility wrapper for the canonical src.youtube_handler module.

``services.youtube.youtube_handler`` remains importable for the diagnostics
route and older service code, but the implementation now lives in
``src.youtube_handler`` (the copy used by the chat path) so URL parsing,
transcript formatting, and yt-dlp timeout handling cannot drift between
copies.
"""

import sys

from src import youtube_handler as _youtube_handler

# Mirror the live module's names here so loaders that keep a direct reference
# to this module object (e.g. importlib.util.spec_from_file_location in tests)
# see the same API as normal imports.
globals().update(
    {k: v for k, v in vars(_youtube_handler).items()
     if not (k.startswith("__") and k.endswith("__"))}
)

# Normal imports resolve straight to the live module.
sys.modules[__name__] = _youtube_handler
