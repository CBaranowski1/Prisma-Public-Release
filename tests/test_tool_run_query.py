"""Regression coverage for retirement of the run_query agent tool."""

from src import agent_tools, tool_parsing, tool_schemas


def test_run_query_is_not_advertised_or_parseable():
    names = {
        schema["function"]["name"]
        for schema in tool_schemas.FUNCTION_TOOL_SCHEMAS
    }
    assert "run_query" not in names
    assert "run_query" not in agent_tools.TOOL_TAGS
    assert "run_query" not in tool_parsing._TOOL_NAME_MAP
    assert (
        tool_schemas.function_call_to_tool_block(
            "run_query",
            '{"definition": {"tables": []}}',
        )
        is None
    )
