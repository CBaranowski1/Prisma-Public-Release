"""Tests for build_images_url — the centralized image-generation endpoint resolver."""

import pytest

from src.endpoint_resolver import build_images_url


def test_openai_style_images_url():
    base = "https://api.openai.com/v1"
    url = build_images_url(base)
    assert url.startswith(base)
    assert url.endswith("/images/generations")


def test_strips_chat_suffix():
    base = "https://api.openai.com/v1/chat/completions"
    url = build_images_url(base)
    assert url == "https://api.openai.com/v1/images/generations"
    assert "chat/completions" not in url


def test_openrouter_style():
    base = "https://openrouter.ai/api/v1"
    url = build_images_url(base)
    assert url == base + "/images/generations"


def test_no_images_endpoint_raises():
    with pytest.raises(ValueError):
        build_images_url("https://api.anthropic.com")
    with pytest.raises(ValueError):
        build_images_url("http://localhost:11434/api")
    with pytest.raises(ValueError):
        build_images_url("https://ollama.com")
