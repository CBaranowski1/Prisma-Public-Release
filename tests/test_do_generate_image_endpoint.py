"""do_generate_image (the live chat-image path in src.ai_interaction) must
resolve its images endpoint through src.endpoint_resolver.build_images_url —
never a chat-URL .replace() chain. Mirrors the mcp_servers/image_gen_server.py
fix (26-03) for the actually-reachable path (presence != integration)."""
import base64
import re
from pathlib import Path

import pytest

from src import ai_interaction

# A 1x1 transparent PNG, base64-encoded.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _FakeResponse:
    def __init__(self, status_code, json_data, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or str(json_data)

    def json(self):
        return self._json


class _CapturingAsyncClient:
    """Stand-in for httpx.AsyncClient supporting `async with ... as client`."""

    def __init__(self, response):
        self._response = response
        self.posted_url = None
        self.posted_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None, **kwargs):
        self.posted_url = url
        self.posted_kwargs = {"json": json, "headers": headers}
        return self._response


@pytest.mark.asyncio
async def test_uses_builder_for_images_url(monkeypatch, tmp_path):
    """do_generate_image derives the images URL via build_images_url — the
    captured request URL must equal normalize_base(url) + '/images/generations'
    with no chat-URL suffix leaked into it."""
    import httpx

    monkeypatch.setattr("core.constants.DATA_DIR", str(tmp_path))

    monkeypatch.setattr(
        ai_interaction,
        "_resolve_model",
        lambda model_spec, owner=None: (
            "https://api.openai.com/v1/chat/completions",
            "gpt-image-1",
            {"Authorization": "Bearer test"},
        ),
    )

    fake_response = _FakeResponse(200, {"data": [{"b64_json": _TINY_PNG_B64}]})
    captured = {}

    def _client_factory(*a, **kw):
        client = _CapturingAsyncClient(fake_response)
        captured["client"] = client
        return client

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

    result = await ai_interaction.do_generate_image("a cat\n")

    assert "error" not in result, result
    assert captured["client"].posted_url == "https://api.openai.com/v1/images/generations"
    assert "chat/completions" not in captured["client"].posted_url


@pytest.mark.asyncio
async def test_no_images_endpoint_errors(monkeypatch):
    """When the resolved provider has no images endpoint (build_images_url
    raises), return an error dict and never attempt a request."""
    import httpx

    monkeypatch.setattr(
        ai_interaction,
        "_resolve_model",
        lambda model_spec, owner=None: (
            "https://api.anthropic.com/v1/messages",
            "claude-3-opus",
            {"x-api-key": "test"},
        ),
    )

    posted = {"called": False}

    class _ExplodingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            posted["called"] = True
            raise AssertionError("must not attempt a request when there is no images endpoint")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _ExplodingClient())

    result = await ai_interaction.do_generate_image("a cat\n")

    assert not posted["called"], "do_generate_image attempted a request despite no images endpoint"
    assert "error" in result
    assert result["error"]


def test_no_replace_chain():
    """Static check: the do_generate_image region no longer contains the
    chat-URL .replace() images derivation, and build_images_url is used."""
    source = Path(ai_interaction.__file__).read_text(encoding="utf-8")
    assert "build_images_url" in source, "expected an import/use of build_images_url"
    assert re.search(r"from\s+src\.endpoint_resolver\s+import[^\n]*build_images_url", source), (
        "expected `from src.endpoint_resolver import ... build_images_url`"
    )
    assert not re.search(r'\.replace\(\s*["\']/chat/completions["\']', source), (
        "found a leftover chat-URL .replace() chain used for images-endpoint derivation"
    )
