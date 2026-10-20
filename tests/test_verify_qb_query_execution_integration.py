"""Execution-spine regression for the retired Query Builder."""

import inspect

from src import agent_tools, tool_execution


def test_query_execution_cannot_enter_the_agent_spine():
    source = inspect.getsource(tool_execution.execute_tool_block)
    assert "run_query" not in agent_tools.TOOL_TAGS
    assert 'tool == "run_query"' not in source
    assert "do_run_query" not in source
