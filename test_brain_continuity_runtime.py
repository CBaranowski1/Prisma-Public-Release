"""Regression coverage for Second Brain continuity and VERA catalog wiring."""

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from routes.chat_routes import _needs_contextual_agent_promotion, _resolve_reasoning_effort
from routes.chat_helpers import _runtime_capability_manifest
from routes.brain_routes import setup_brain_routes
import routes.vera_agents_routes as vera
from src.agent_loop import _compact_tool_result_for_model, _recent_context_for_retrieval
from src.tool_index import ToolIndex
from src.tool_implementations import _normalize_brain_app_api_call, do_app_api


_AGENTS = [
    {
        "id": "specialist_postgresql",
        "name": "PostgreSQL Specialist",
        "role": "specialist",
        "squad": "build",
        "brain": "precision-audit",
        "activation_tokens": ["postgres", "postgresql", "database"],
        "model_policy": {"default": "local"},
        "tools": [],
    },
    {
        "id": "reviewer_accessibility",
        "name": "Accessibility Reviewer",
        "role": "reviewer",
        "squad": "quality",
        "brain": "llm-council",
        "activation_tokens": ["accessibility", "wcag"],
        "model_policy": {"default": "local"},
        "tools": [],
    },
]


def _catalog():
    return {
        "agents": _AGENTS,
        "by_squad": {"build": ["specialist_postgresql"], "quality": ["reviewer_accessibility"]},
        "by_brain": {"precision-audit": ["specialist_postgresql"], "llm-council": ["reviewer_accessibility"]},
        "errors": [],
    }


def test_terse_followup_inherits_recent_brain_topic():
    history = [
        {"role": "user", "content": "Can you verify the VERA Brain search and skill catalog?"},
        {"role": "assistant", "content": "I need to test the brain provider."},
    ]
    assert _needs_contextual_agent_promotion("hello", history) is True
    assert _needs_contextual_agent_promotion("did you fix it?", []) is True
    assert _needs_contextual_agent_promotion("hello", [{"role": "user", "content": "Tell me a joke"}]) is False


def test_adaptive_reasoning_router_preserves_explicit_choice_and_limits_auto():
    assert _resolve_reasoning_effort("max", "hello") == ("max", "user-selected max")
    assert _resolve_reasoning_effort("auto", "hello") == ("low", "auto: direct conversation")
    effort, reason = _resolve_reasoning_effort("auto", "Research and compare the system architecture")
    assert effort == "high"
    assert "complex" in reason
    assert _resolve_reasoning_effort("auto", "hello", agent_mode=True)[0] == "high"


def test_capability_manifest_distinguishes_skills_from_legacy_profiles():
    class Registry:
        def active(self):
            return []

    class Skills:
        def load(self, owner=None):
            assert owner == "admin"
            return [{"name": "second-brain-operator"}, {"name": "vera-agent-catalog"}]

    class Processor:
        provider_registry = Registry()
        skills_manager = Skills()

    class Session:
        model = "glm-5.3-flash"

    with patch.object(vera, "catalog_summary", lambda: {
        "unique_profiles": 78, "source_configs": 94, "brains": ["precision-audit"],
    }):
        manifest = _runtime_capability_manifest(
            sess=Session(), chat_processor=Processor(), owner="admin",
            agent_mode=True, incognito=False, profile=None, skills_enabled=True,
            used_memories=[], rag_sources=[], brain_used=[],
            reasoning_requested="auto", reasoning_effective="high",
            reasoning_route="auto: agent/tool workflow",
        )
    assert "Legacy brain skills (VERA profiles): 78" in manifest
    assert "94 validated YAML" in manifest
    assert "Prisma SKILL.md procedures visible to this user: 2" in manifest
    assert "requested=auto, effective=high" in manifest


def test_agent_loop_no_longer_builds_a_second_global_skill_index():
    source = open(__import__("src.agent_loop", fromlist=["__file__"]).__file__, encoding="utf-8").read()
    assert "owner=None, active_toolsets=active_tools" not in source
    assert "Inventory questions are already answered by the trusted runtime" in source


def test_second_brain_tool_set_keeps_history_search():
    index = ToolIndex.__new__(ToolIndex)
    tools = index.get_tools_for_query("Check our second brain and stored skills")
    assert {"app_api", "manage_memory", "manage_skills", "search_chats"} <= tools


def test_model_facing_tool_result_is_bounded_without_losing_header():
    original = "### app_api\n" + ("x" * 9000)
    compact = _compact_tool_result_for_model(original, 2400)
    assert compact.startswith("### app_api")
    assert "9000" not in compact  # total includes the header, not a magic fixture value
    assert len(compact) < 2550
    assert "Tool result compacted" in compact


def test_tool_retrieval_reaches_past_terse_followups():
    messages = [
        {"role": "user", "content": "Verify the VERA Brain roster and stored skills."},
        {"role": "assistant", "content": "Checking."},
        {"role": "user", "content": "did you stall"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "I cannot check in Chat mode."},
        {"role": "user", "content": "hello - retry it now"},
    ]
    query = _recent_context_for_retrieval(messages)
    assert "VERA Brain" in query
    assert "retry it now" in query


def test_brain_app_api_normalizes_read_only_model_variants():
    method, body, query = _normalize_brain_app_api_call(
        "POST", "/api/brain/search",
        {"query": "VERA agents", "provider": "brain:vera-brain"}, None,
    )
    assert method == "GET"
    assert body is None
    assert query == {"q": "VERA agents", "provider": "brain:vera-brain"}

    method, body, query = _normalize_brain_app_api_call(
        "GET", "/api/brain/search", None, {"query": "profiles"},
    )
    assert method == "GET"
    assert query == {"q": "profiles", "provider": "brain:all"}


def test_app_api_initializes_request_data_before_normalizing():
    result = asyncio.run(do_app_api(json.dumps({
        "action": "call",
        "method": "POST",
        "path": "/api/brain/agents",
        "body": {"query": "postgres"},
    })))
    assert result["exit_code"] == 0
    assert result["status_code"] == 200
    assert "runtime" in result["json"]


def test_catalog_documents_are_searchable_and_actionable():
    with patch.object(vera, "_cached", _catalog):
        rows = vera.catalog_documents("postgres")
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "specialist_postgresql"
    assert rows[0]["source_brain"] == "precision-audit"
    assert rows[0]["kind"] == "vera_profile"
    with patch.object(vera, "_cached", _catalog):
        assert len(vera.catalog_documents("VERA agents")) == 2


def test_vera_provider_search_includes_virtual_profile_documents():

    class Provider:
        async def categories(self):
            return ["knowledge"]

        async def search(self, query, category=None):
            return []

    class Registry:
        def get(self, provider_id):
            assert provider_id == "brain:vera-brain"
            return Provider()

        def all(self):
            return []

    with patch.object(vera, "_cached", _catalog):
        app = FastAPI()
        app.include_router(setup_brain_routes(Registry()))
        client = TestClient(app)

        categories = client.get("/api/brain/categories", params={"provider": "brain:vera-brain"})
        assert categories.status_code == 200
        assert "vera-agents" in categories.json()

        result = client.get(
            "/api/brain/search",
            params={"provider": "brain:vera-brain", "q": "postgres"},
        )
        assert result.status_code == 200
        rows = result.json()
        assert [row["agent_id"] for row in rows] == ["specialist_postgresql"]


def test_agent_endpoint_q_really_filters():
    with patch.object(vera, "_cached", _catalog):
        app = FastAPI()
        app.include_router(vera.router)
        client = TestClient(app)
        result = client.get("/api/brain/agents", params={"q": "wcag"})
        assert result.status_code == 200
        assert result.json()["count"] == 1
        assert result.json()["agents"][0]["id"] == "reviewer_accessibility"


if __name__ == "__main__":
    test_terse_followup_inherits_recent_brain_topic()
    test_adaptive_reasoning_router_preserves_explicit_choice_and_limits_auto()
    test_capability_manifest_distinguishes_skills_from_legacy_profiles()
    test_agent_loop_no_longer_builds_a_second_global_skill_index()
    test_second_brain_tool_set_keeps_history_search()
    test_model_facing_tool_result_is_bounded_without_losing_header()
    test_tool_retrieval_reaches_past_terse_followups()
    test_brain_app_api_normalizes_read_only_model_variants()
    test_app_api_initializes_request_data_before_normalizing()
    test_catalog_documents_are_searchable_and_actionable()
    test_vera_provider_search_includes_virtual_profile_documents()
    test_agent_endpoint_q_really_filters()
    print("12 brain continuity and frontier-routing regression checks passed")
