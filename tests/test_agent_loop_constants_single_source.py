"""GRAY-AGT-02: identity test proving the agent-loop/tool-execution limits
are single-sourced in core/constants.py.

Uses `is` (object identity), not `==`, throughout -- a future re-divergence
into a separate-but-equal literal (e.g. someone re-adding
`MAX_OUTPUT_CHARS = 10_000` locally in a consumer module) coincidentally has
the same VALUE but is NOT the same OBJECT, so `is` catches it while `==`
would not.
"""

import sys
from unittest.mock import MagicMock

import core.constants as constants
import src.agent_tools as agent_tools
import src.tool_execution as tool_execution
import src.tool_implementations as tool_implementations


def test_max_output_chars_single_sourced():
    assert agent_tools.MAX_OUTPUT_CHARS is constants.MAX_OUTPUT_CHARS
    assert tool_execution.MAX_OUTPUT_CHARS is constants.MAX_OUTPUT_CHARS
    assert tool_implementations.MAX_OUTPUT_CHARS is constants.MAX_OUTPUT_CHARS


def test_max_read_chars_single_sourced():
    assert agent_tools.MAX_READ_CHARS is constants.MAX_READ_CHARS
    assert tool_execution.MAX_READ_CHARS is constants.MAX_READ_CHARS
    assert tool_implementations.MAX_READ_CHARS is constants.MAX_READ_CHARS


def test_max_agent_rounds_single_sourced():
    assert agent_tools.MAX_AGENT_ROUNDS is constants.MAX_AGENT_ROUNDS


def test_dead_timeout_constants_removed_from_agent_tools():
    assert not hasattr(agent_tools, "SHELL_TIMEOUT")
    assert not hasattr(agent_tools, "PYTHON_TIMEOUT")


def test_agent_loop_max_agent_rounds_reexport_chain_intact():
    # agent_loop.py:42 does `from src.agent_tools import (... MAX_AGENT_ROUNDS)`.
    # Importing src.agent_loop pulls in the full app dependency stack (DB,
    # settings, tool_index, etc). Import it directly first; only fall back to
    # a minimal heavy-dependency stub dance (mirroring
    # tests/test_agent_loop_verify_gate.py:39-64) if the bare import fails in
    # this environment, so the identity assertion still runs rather than
    # being silently skipped whenever possible.
    _mocked_imports = [
        "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext",
        "sqlalchemy.ext.declarative", "sqlalchemy.ext.hybrid",
        "sqlalchemy.sql", "sqlalchemy.sql.expression",
        "src.database", "core.models", "core.database",
    ]
    injected_stubs = {}
    try:
        import src.agent_loop as agent_loop
    except Exception:
        for mod in _mocked_imports:
            if mod not in sys.modules:
                stub = MagicMock()
                sys.modules[mod] = stub
                injected_stubs[mod] = stub
        try:
            import src.agent_loop as agent_loop
        finally:
            for mod, stub in injected_stubs.items():
                if sys.modules.get(mod) is stub:
                    sys.modules.pop(mod, None)

    assert agent_loop.MAX_AGENT_ROUNDS is constants.MAX_AGENT_ROUNDS
