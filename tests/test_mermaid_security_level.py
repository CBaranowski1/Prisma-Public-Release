"""Regression guard for Mermaid securityLevel (audit P2).

Mermaid diagrams are rendered from assistant- and user-authored markdown, i.e.
untrusted content. securityLevel:'loose' lets diagram source inject raw HTML and
wire click handlers — a DOM-XSS / sanitizer-bypass vector. The initializer must
use 'strict' (or 'sandbox'), never 'loose'.
"""

import re
from pathlib import Path


_REPO = Path(__file__).resolve().parent.parent
_MARKDOWN_JS = _REPO / "static" / "js" / "markdown.js"


def test_mermaid_not_initialized_in_loose_mode():
    src = _MARKDOWN_JS.read_text(encoding="utf-8")
    assert "securityLevel: 'loose'" not in src, (
        "Mermaid must not run in 'loose' mode over untrusted chat markdown"
    )
    assert 'securityLevel: "loose"' not in src


def test_mermaid_initialized_in_strict_or_sandbox_mode():
    src = _MARKDOWN_JS.read_text(encoding="utf-8")
    # The mermaid.initialize(...) call must pin a safe securityLevel.
    assert re.search(r"securityLevel:\s*['\"](strict|sandbox)['\"]", src), (
        "Mermaid initializer must set securityLevel to 'strict' or 'sandbox'"
    )
