import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.mcp_routes as mcp_routes_mod
from src.mcp_manager import (
    ALL_TOOLS_DISABLED,
    McpManager,
    _format_mcp_connection_error,
    parse_disabled_tools,
)


def test_playwright_mcp_connection_error_includes_install_hint():
    msg = _format_mcp_connection_error(
        "Browser (Playwright)",
        "npx",
        ["-y", "@playwright/mcp@latest", "--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "Browser MCP could not start" in msg
    assert "npx -y @playwright/mcp@latest --version" in msg
    assert "restart Prisma" in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


# ---------------------------------------------------------------------------
# SEC-15 (D-02): parse_disabled_tools fail-closed / wrong-shape rejection
# ---------------------------------------------------------------------------

def test_disabled_tools_corrupt_json_returns_sentinel_not_empty_set(caplog):
    """Corrupt (non-JSON) disabled_tools must fail CLOSED: the sentinel
    (None), never set() -- an empty set would silently reproduce the
    original fail-open bug (Pitfall 3)."""
    with caplog.at_level(logging.ERROR, logger="src.mcp_manager"):
        result = parse_disabled_tools('{"broken":')

    assert result is None
    assert result != set()
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_disabled_tools_shape_bare_string_rejected(caplog):
    """A JSON string (not a list) must NOT become a per-character set via
    set(parsed) -- that is the char-set bug this plan closes."""
    with caplog.at_level(logging.ERROR, logger="src.mcp_manager"):
        result = parse_disabled_tools('"tool_a"')

    assert result is None
    assert result != set("tool_a")  # would be {'t','o','l','_','a'} if buggy
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_disabled_tools_shape_non_str_elements_rejected(caplog):
    """A list containing non-string elements is wrong-shape too."""
    with caplog.at_level(logging.ERROR, logger="src.mcp_manager"):
        result = parse_disabled_tools("[1, 2]")

    assert result is None
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_disabled_tools_shape_valid_list_returns_exact_set():
    assert parse_disabled_tools('["a","b"]') == {"a", "b"}


def test_disabled_tools_shape_none_and_empty_are_not_failures():
    """Nothing-configured is not a failure -- must return set(), not the
    fail-closed sentinel."""
    assert parse_disabled_tools(None) == set()
    assert parse_disabled_tools("") == set()


# ---------------------------------------------------------------------------
# SEC-15: three-consumer contract -- ALL_TOOLS_DISABLED sentinel hides every
# tool of a marked server via all three existing read paths, with zero edits
# to those consumer methods.
# ---------------------------------------------------------------------------

def _mgr_with_stub_server(server_id="srv1"):
    mgr = McpManager()
    mgr._tools[server_id] = [
        {"name": "tool_a", "description": "does a", "input_schema": {}},
        {"name": "tool_b", "description": "does b", "input_schema": {}},
    ]
    mgr._connections[server_id] = {"status": "connected", "name": "Stub Server"}
    return mgr


def test_disabled_tools_three_consumer_contract_hides_all_tools_when_sentinel():
    mgr = _mgr_with_stub_server("srv1")
    disabled_map = {"srv1": ALL_TOOLS_DISABLED}

    schemas = mgr.get_all_openai_schemas(disabled_map)
    assert schemas == []

    tools = mgr.get_all_tools(disabled_map)
    assert tools  # tools were discovered at all
    assert all(t["is_disabled"] for t in tools)

    # get_tool_descriptions_for_prompt's cache-key line does frozenset(v) on
    # every disabled_map value -- must not raise on the sentinel, and must
    # produce no visible tool names for a fully-disabled server.
    desc = mgr.get_tool_descriptions_for_prompt(disabled_map)
    assert "tool_a" not in desc
    assert "tool_b" not in desc


def test_disabled_tools_sentinel_contains_everything_and_not_empty_set():
    assert "any_tool" in ALL_TOOLS_DISABLED
    assert ALL_TOOLS_DISABLED != set()
    converted = frozenset(ALL_TOOLS_DISABLED)  # must not raise
    assert isinstance(converted, frozenset)


# ---------------------------------------------------------------------------
# SEC-15: write-site element-type validation (PATCH /servers/{id}/tools)
# ---------------------------------------------------------------------------

def _mcp_write_handler():
    for r in mcp_routes_mod.setup_mcp_routes(McpManager()).routes:
        methods = getattr(r, "methods", None) or set()
        if "/servers/{server_id}/tools" in getattr(r, "path", "") and "PATCH" in methods:
            return r.endpoint
    raise KeyError("PATCH /servers/{server_id}/tools route not found")


class _FakeJsonRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _stub_db_session(monkeypatch, srv):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = srv
    monkeypatch.setattr(mcp_routes_mod, "SessionLocal", lambda: session)
    monkeypatch.setattr(mcp_routes_mod, "require_admin", lambda request: None)
    return session


def test_disabled_tools_write_rejects_non_string_element(monkeypatch):
    """The write site (PATCH disabled tools) must reject a list containing
    a non-string element with HTTP 400 -- before it ever reaches the DB."""
    srv = SimpleNamespace(id="srv1", disabled_tools=None)
    _stub_db_session(monkeypatch, srv)

    handler = _mcp_write_handler()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(handler(server_id="srv1", request=_FakeJsonRequest({"disabled": ["ok", 123]})))

    assert exc.value.status_code == 400
    assert srv.disabled_tools is None  # never persisted


def test_disabled_tools_write_accepts_valid_list_of_strings(monkeypatch):
    """Happy path: a well-formed list[str] still persists unchanged."""
    srv = SimpleNamespace(id="srv1", disabled_tools=None)
    _stub_db_session(monkeypatch, srv)

    handler = _mcp_write_handler()
    result = asyncio.run(
        handler(server_id="srv1", request=_FakeJsonRequest({"disabled": ["tool_a", "tool_b"]}))
    )

    assert result == {"id": "srv1", "disabled_count": 2}
    assert json.loads(srv.disabled_tools) == ["tool_a", "tool_b"]


# ---------------------------------------------------------------------------
# WR-01 (SEC-15 hardening): the MCP admin DISPLAY endpoints must reuse the
# fail-closed parse_disabled_tools -- a corrupt disabled_tools value must
# degrade to "all disabled" in the response body, never raise/500.
# ---------------------------------------------------------------------------

def _mcp_get_handler(mgr, path: str, exact: bool = True):
    """Return the GET endpoint for `path`, registered against `mgr`.

    `setup_mcp_routes` decorates onto a shared MODULE-LEVEL `router`, so its
    `.routes` list keeps growing across every call in the test session --
    walk in reverse to pick the most-recently-registered closure (the one
    bound to the `mgr` just passed in), never a stale one from an earlier
    test's McpManager instance.
    """
    routes = mcp_routes_mod.setup_mcp_routes(mgr).routes
    for r in reversed(routes):
        methods = getattr(r, "methods", None) or set()
        r_path = getattr(r, "path", "")
        matched = (r_path == path) if exact else (path in r_path)
        if matched and "GET" in methods:
            return r.endpoint
    raise KeyError(f"GET {path} route not found")


class _FakeRequest:
    """Minimal stand-in for fastapi.Request -- routes here only pass it
    straight into require_admin, which is monkeypatched to a no-op."""


def test_list_servers_corrupt_disabled_tools_reports_all_disabled_not_500(monkeypatch, caplog):
    corrupt_srv = SimpleNamespace(
        id="srv-corrupt", name="Corrupt Server", transport="stdio", command="x",
        args=None, env=None, url=None, is_enabled=True, oauth_config=None,
        disabled_tools='{"broken":',
    )
    session = MagicMock()
    session.query.return_value.all.return_value = [corrupt_srv]
    monkeypatch.setattr(mcp_routes_mod, "SessionLocal", lambda: session)
    monkeypatch.setattr(mcp_routes_mod, "require_admin", lambda request: None)

    mgr = _mgr_with_stub_server("srv-corrupt")  # 2 tools registered
    monkeypatch.setattr(
        mgr, "get_server_status",
        lambda server_id: {"status": "connected", "tool_count": 2},
    )

    handler = _mcp_get_handler(mgr, "/api/mcp/servers")

    with caplog.at_level(logging.WARNING, logger="routes.mcp_routes"):
        result = handler(request=_FakeRequest())

    assert len(result) == 1
    entry = result[0]
    assert entry["id"] == "srv-corrupt"
    assert entry["disabled_tools_corrupt"] is True
    assert entry["tool_count"] == 2
    assert entry["disabled_tool_count"] == 2
    assert entry["enabled_tool_count"] == 0
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_list_servers_wellformed_disabled_tools_is_not_corrupt(monkeypatch):
    good_srv = SimpleNamespace(
        id="srv-good", name="Good Server", transport="stdio", command="x",
        args=None, env=None, url=None, is_enabled=True, oauth_config=None,
        disabled_tools='["tool_a"]',
    )
    session = MagicMock()
    session.query.return_value.all.return_value = [good_srv]
    monkeypatch.setattr(mcp_routes_mod, "SessionLocal", lambda: session)
    monkeypatch.setattr(mcp_routes_mod, "require_admin", lambda request: None)

    handler = _mcp_get_handler(McpManager(), "/api/mcp/servers")
    result = handler(request=_FakeRequest())

    assert result[0]["disabled_tools_corrupt"] is False
    assert result[0]["disabled_tool_count"] == 1


def test_list_server_tools_corrupt_disabled_tools_reports_all_disabled_not_500(monkeypatch, caplog):
    corrupt_srv = SimpleNamespace(id="srv-corrupt", disabled_tools='{"broken":')
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = corrupt_srv
    monkeypatch.setattr(mcp_routes_mod, "SessionLocal", lambda: session)
    monkeypatch.setattr(mcp_routes_mod, "require_admin", lambda request: None)

    mgr = _mgr_with_stub_server("srv-corrupt")
    handler = _mcp_get_handler(mgr, "/api/mcp/servers/{server_id}/tools")

    with caplog.at_level(logging.WARNING, logger="routes.mcp_routes"):
        result = handler(server_id="srv-corrupt", request=_FakeRequest())

    assert result, "tools were discovered at all"
    assert all(t["is_disabled"] for t in result), "corrupt disabled_tools must show all tools disabled"
    assert any(r.levelname == "WARNING" for r in caplog.records)
