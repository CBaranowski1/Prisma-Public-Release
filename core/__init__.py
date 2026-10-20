# core/__init__.py
# (touch: force sync)
"""
Chat Core — the essential chat experience.

This package contains only what's needed for:
- Streaming LLM responses
- Session management
- Model routing
- Authentication
"""

from .auth import AuthManager
from .constants import *
from .middleware import SecurityHeadersMiddleware
from .exceptions import (
    SessionNotFoundError,
    InvalidFileUploadError,
    LLMServiceError,
    WebSearchError,
)
from .models import Session, ChatMessage
from .session_manager import SessionManager

# LLM primitives (llm_call, llm_call_async, stream_llm, list_model_ids,
# normalize_model_id, LLMConfig) used to be re-exported here eagerly, which
# created a core -> src.llm_core import cycle at module load. No in-repo code
# imports them from ``core`` anymore — use ``src.llm_core`` directly. The lazy
# PEP 562 hook below keeps ``core.llm_call`` etc. working for any out-of-tree
# callers without importing src at core's import time.
_LLM_REEXPORTS = (
    "llm_call",
    "llm_call_async",
    "stream_llm",
    "list_model_ids",
    "normalize_model_id",
    "LLMConfig",
)


def __getattr__(name):
    if name in _LLM_REEXPORTS:
        from src import llm_core
        return getattr(llm_core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # LLM (lazy re-exports; see __getattr__ above)
    "llm_call",
    "llm_call_async",
    "stream_llm",
    "list_model_ids",
    "normalize_model_id",
    "LLMConfig",
    # Auth
    "AuthManager",
    # Middleware
    "SecurityHeadersMiddleware",
    # Exceptions
    "SessionNotFoundError",
    "InvalidFileUploadError",
    "LLMServiceError",
    "WebSearchError",
    # Models
    "Session",
    "ChatMessage",
    "SessionManager",
]
