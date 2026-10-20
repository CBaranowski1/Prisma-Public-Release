"""image_gen_server must write generated images under core.constants.DATA_DIR
(never a cwd-relative path) and must resolve its images endpoint through
src.endpoint_resolver.build_images_url — never a chat-URL .replace() chain.

Under Docker/frozen builds the process cwd and DATA_DIR diverge; a hardcoded
relative `data/generated_images` directory silently strands generated images
outside the writable data volume.
"""
import base64
import re
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp_servers.image_gen_server as igs

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


class _FakeAsyncClient:
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
async def test_b64_write_under_data_dir(monkeypatch, tmp_path):
    """The b64-image write branch writes under DATA_DIR/generated_images, not cwd."""
    import httpx

    # Route DATA_DIR (both the canonical source and any module-level binding)
    # to a tmp_path so we can assert on exactly where the file landed.
    monkeypatch.setattr("core.constants.DATA_DIR", str(tmp_path))
    if hasattr(igs, "DATA_DIR"):
        monkeypatch.setattr(igs, "DATA_DIR", str(tmp_path), raising=False)

    # Isolate cwd too: if the fix regresses to a cwd-relative literal, the
    # stray dir lands inside this fake cwd (a sibling of tmp_path) instead of
    # polluting the real repo's data/ directory or colliding with pre-existing
    # generated images from prior dev/test runs.
    fake_cwd = tmp_path / "_fake_cwd"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)

    monkeypatch.setattr(
        "src.ai_interaction._resolve_model",
        lambda model_spec, owner=None: (
            "https://api.openai.com/v1/chat/completions",
            "gpt-image-1",
            {"Authorization": "Bearer test"},
        ),
    )

    fake_response = _FakeResponse(200, {"data": [{"b64_json": _TINY_PNG_B64}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _FakeAsyncClient(fake_response))

    result = await igs.call_tool("generate_image", {"prompt": "a red apple"})

    assert result and "Error" not in result[0].text, result[0].text if result else "no result"

    generated_dir = tmp_path / "generated_images"
    assert generated_dir.is_dir(), "expected DATA_DIR/generated_images to be created"
    written = list(generated_dir.glob("*.png"))
    assert len(written) == 1, f"expected exactly one written PNG, found {written}"

    # No stray cwd-relative `data/generated_images` should have been created
    # under the fake (isolated) cwd.
    cwd_stray = fake_cwd / "data" / "generated_images"
    assert not cwd_stray.exists(), (
        "a stray file appeared under the cwd-relative data/generated_images dir"
    )


@pytest.mark.asyncio
async def test_endpoint_uses_builder(monkeypatch):
    """When the resolved provider has no images endpoint, return a clear error
    instead of attempting a mangled request."""
    import httpx

    monkeypatch.setattr(
        "src.ai_interaction._resolve_model",
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

    result = await igs.call_tool("generate_image", {"prompt": "a red apple"})

    assert not posted["called"], "image_gen_server attempted a request despite no images endpoint"
    assert result, "expected a TextContent error result"
    text = result[0].text.lower()
    assert "error" in text
    assert "image" in text and ("endpoint" in text or "configure" in text)


def test_no_replace_chain():
    """Static check: no chained chat-URL .replace() images-URL derivation remains,
    and build_images_url is imported from src.endpoint_resolver."""
    source = Path(igs.__file__).read_text(encoding="utf-8")
    assert "build_images_url" in source, "expected an import/use of build_images_url"
    assert re.search(r"from\s+src\.endpoint_resolver\s+import[^\n]*build_images_url", source), (
        "expected `from src.endpoint_resolver import ... build_images_url`"
    )
    assert not re.search(r'\.replace\(\s*["\']/chat/completions["\']', source), (
        "found a leftover chat-URL .replace() chain used for images-endpoint derivation"
    )
