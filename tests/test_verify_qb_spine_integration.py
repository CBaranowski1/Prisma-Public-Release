"""Integration guard proving run_query has no execution-spine dispatch."""

import inspect

from src import tool_execution


def test_run_query_dispatch_branch_is_retired():
    source = inspect.getsource(tool_execution.execute_tool_block)
    assert 'tool == "run_query"' not in source
    assert "do_run_query" not in source
