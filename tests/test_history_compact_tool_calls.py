from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage
import routes.session_routes as session_routes


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session
        self.saved = False

    def get_session(self, session_id):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session

    def save_sessions(self):
        self.saved = True

    def replace_messages(self, session_id, messages):
        if session_id != self.session.id:
            return False
        self.session.history = messages
        self.session.message_count = len(messages)
        self.saved = True
        return True


class _FakeSession:
    id = "session-1"
    name = "Tool session"
    endpoint_url = "http://example.test/v1"
    model = "test-model"
    headers = {}

    def __init__(self, history):
        self.history = history
        self.message_count = len(history)

    def get_context_messages(self):
        return [
            msg.to_dict() if isinstance(msg, ChatMessage) else msg
            for msg in self.history
        ]


def _compact_prompt_for(monkeypatch, history):
    captured = {}

    async def fake_llm_call_async(endpoint_url, model, messages, **kwargs):
        captured["messages"] = messages
        return "Summary text"

    monkeypatch.setattr(
        session_routes,
        "_verify_session_owner",
        lambda request, session_id, session_manager=None: None,
    )

    import src.endpoint_resolver as endpoint_resolver
    import src.llm_core as llm_core

    monkeypatch.setattr(
        endpoint_resolver,
        "resolve_endpoint",
        lambda kind, owner=None: (None, None, {}),
    )
    monkeypatch.setattr(llm_core, "llm_call_async", fake_llm_call_async)

    session = _FakeSession(history)
    manager = _FakeSessionManager(session)
    app = FastAPI()
    app.include_router(session_routes.setup_session_routes(manager, {}))

    response = TestClient(app).post("/api/session/session-1/compact")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert manager.saved is True
    return captured["messages"][1]["content"]


def test_manual_compact_tolerates_chatmessage_with_none_content(monkeypatch):
    compact_prompt = _compact_prompt_for(
        monkeypatch,
        [
            ChatMessage(role="user", content="start"),
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
    )
    assert "ASSISTANT: None" not in compact_prompt
    assert "ASSISTANT: " in compact_prompt


def test_manual_compact_tolerates_dict_message_with_none_content(monkeypatch):
    compact_prompt = _compact_prompt_for(
        monkeypatch,
        [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": None},
            ChatMessage(role="tool", content="tool result"),
            ChatMessage(role="assistant", content="done"),
            ChatMessage(role="user", content="next"),
            ChatMessage(role="assistant", content="final"),
        ],
    )
    assert "ASSISTANT: None" not in compact_prompt
    assert "ASSISTANT: " in compact_prompt
