"""Detect file paths in LLM response text and build IDE open hints."""
import re

# Matches /absolute/unix/path.ext or C:\Windows\path.ext, with optional :line
_UNIX_PATH_RE = re.compile(
    r'(?<![:/\w])(/(?:[\w.\-]+/)*[\w.\-]+\.\w{1,10})(?::(\d+))?'
)
_WIN_PATH_RE = re.compile(
    r'([A-Za-z]:\\(?:[\w.\- ]+\\)*[\w.\- ]+\.\w{1,10})(?::(\d+))?'
)
# Exclude URLs — paths that appear after :// are not file paths
_URL_RE = re.compile(r'https?://')


def extract_ide_hint(text: str) -> dict | None:
    """Return the first file-path hint found in `text`, or None."""
    if _URL_RE.search(text):
        # Strip URL substrings before scanning to avoid false positives
        text = _URL_RE.sub("__URL__", text)

    for pattern in (_UNIX_PATH_RE, _WIN_PATH_RE):
        m = pattern.search(text)
        if m:
            path = m.group(1)
            line = int(m.group(2)) if m.group(2) else None
            return {"path": path, "line": line}
    return None
