"""
_common.py

Shared constants and helpers for built-in MCP servers.
"""

MAX_OUTPUT_CHARS = 10_000
MAX_READ_CHARS = 20_000
# SHELL_TIMEOUT / PYTHON_TIMEOUT below are DEAD — orphaned from the built-in
# MCP servers this module backed, which were deleted in favor of native
# execution (see src/tool_execution.py:576-579 docstring). Not imported by
# any live code path (GRAY-AGT-02 audit). Do not treat as a source of truth
# for shell/python timeouts — that's core.constants.TOOL_WALL_CLOCK_TIMEOUT_SECONDS
# plus src/tool_execution.py's DEFAULT_BASH_TIMEOUT/DEFAULT_PYTHON_TIMEOUT.
SHELL_TIMEOUT = 60
PYTHON_TIMEOUT = 30
SEARCH_TIMEOUT = 30


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate text to *limit* characters with a suffix note."""
    if not isinstance(text, str):
        # Tool output is occasionally None or a non-string; len(None) would
        # raise. Coerce so this shared helper never crashes a tool response.
        text = "" if text is None else str(text)
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated, {len(text)} chars total)"
    return text
