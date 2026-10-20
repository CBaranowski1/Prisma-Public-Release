"""Unit tests for `fence_untrusted_text`, the text-only fence primitive
extracted from `untrusted_context_message` (Phase 18, Plan 01).

This is a behavior-preserving refactor: `untrusted_context_message` now
delegates to `fence_untrusted_text` for its `content` field. These tests
pin that the extraction did not change any observable behavior.
"""

from src.prompt_security import (
    UNTRUSTED_CONTEXT_HEADER,
    _UNTRUSTED_START_MARKER,
    _UNTRUSTED_END_MARKER,
    fence_untrusted_text,
    untrusted_context_message,
)


def test_fence_untrusted_text_returns_str_with_header_source_markers_and_content_in_order():
    result = fence_untrusted_text("brain notes", "hello")

    assert isinstance(result, str)

    header_idx = result.index(UNTRUSTED_CONTEXT_HEADER)
    source_idx = result.index("Source: brain notes")
    start_idx = result.index(_UNTRUSTED_START_MARKER)
    content_idx = result.index("hello")
    end_idx = result.index(_UNTRUSTED_END_MARKER)

    assert header_idx < source_idx < start_idx < content_idx < end_idx
    assert result.rstrip().endswith(_UNTRUSTED_END_MARKER)


def test_fence_untrusted_text_coerces_none_content_to_empty_body():
    result = fence_untrusted_text("brain notes", None)

    assert isinstance(result, str)
    assert UNTRUSTED_CONTEXT_HEADER in result
    assert "Source: brain notes" in result
    assert _UNTRUSTED_START_MARKER in result
    assert _UNTRUSTED_END_MARKER in result
    # Empty body: start marker immediately followed by end marker (with only
    # the newline separators the template inserts), no content in between.
    between = result.split(_UNTRUSTED_START_MARKER, 1)[1].split(_UNTRUSTED_END_MARKER, 1)[0]
    assert between.strip() == ""


def test_untrusted_context_message_content_equals_fence_untrusted_text_output():
    for content in ("hello", "", None, "multi\nline\ntext", 12345):
        label = "brain notes"
        assert untrusted_context_message(label, content)["content"] == fence_untrusted_text(label, content)


def test_untrusted_context_message_dict_shape_unchanged():
    msg = untrusted_context_message("brain notes", "hello")

    assert msg["role"] == "user"
    assert msg["metadata"] == {"trusted": False, "source": "brain notes"}
    assert set(msg.keys()) == {"role", "content", "metadata"}
