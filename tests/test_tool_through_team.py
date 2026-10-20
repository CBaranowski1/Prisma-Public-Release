"""The retired run_query tool cannot be assigned to a live council seat."""

from src import agent_tools
from src.settings import DEFAULT_COUNCIL


def test_default_councils_do_not_expose_run_query():
    assert "run_query" not in agent_tools.TOOL_TAGS
    assigned = {
        tool
        for council in DEFAULT_COUNCIL["councils"]
        for seat in council["seats"]
        for tool in seat.get("tools", [])
    }
    assert "run_query" not in assigned
