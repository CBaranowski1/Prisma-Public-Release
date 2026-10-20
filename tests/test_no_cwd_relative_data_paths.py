"""Guard: no runtime module may build a data path relative to the current
working directory.

Frozen PyInstaller bundles launch with an arbitrary cwd (whatever the desktop
shell sets). Any `Path("data/...")`, `os.path.join("data", ...)`, or
`os.environ.get("DATA_DIR", "data")` fallback resolves against that cwd and
silently creates a stray `data/` tree there instead of using
`core.constants.DATA_DIR` (the frozen-aware, absolute data root). This test
fails loudly if such a literal is reintroduced.

See memory: project-bundle-path-divergence.
"""
import re
from pathlib import Path

# Repo root == two levels up from this test file.
_ROOT = Path(__file__).resolve().parent.parent

# Runtime packages that run inside the bundled prism-server process. Excludes
# tests/, scripts/, standalone mcp_servers/, integrations/, and the dev-only
# setup.py installer (none of which run in the packaged server's hot path).
_RUNTIME_TARGETS = [
    "app.py",
    "server_main.py",
    "routes",
    "src",
    "services",
    "core",
    "companion",
    "mcp_servers",
]

# A cwd-relative data path literal: a quote immediately followed by `data/`.
# (JSON keys like ``data.get("data")`` and SSE ``f"data: {...}"`` never match
# because they lack the trailing slash.)
_RE_DATA_SLASH = re.compile(r"""["']data/""")
# os.path.join("data", ...) — the cwd-relative join form.
_RE_JOIN_DATA = re.compile(r"""join\(\s*["']data["']""")
# os.environ.get("DATA_DIR", "data") — cwd fallback when the env var is unset.
_RE_ENV_FALLBACK = re.compile(r"""DATA_DIR["']\s*,\s*["']data["']""")

_BANNED = (_RE_DATA_SLASH, _RE_JOIN_DATA, _RE_ENV_FALLBACK)

# `Path(...) / "data"` — a pathlib join-form cwd/__file__-relative data path
# (e.g. `Path(__file__).resolve().parent.parent / "data"`). NOT caught by any
# of the three regexes above (they require a trailing slash inside the
# quoted literal, which this form lacks). Scoped to mcp_servers/ only: a few
# existing files under routes/ and src/ (out of scope for this phase) use
# this same join shape and would be flagged as false positives if the check
# ran against the full _RUNTIME_TARGETS set.
_RE_JOIN_FORM = re.compile(r"""/\s*["']data["']""")


def _iter_mcp_servers_py_files():
    p = _ROOT / "mcp_servers"
    if p.is_dir():
        yield from p.rglob("*.py")


def test_no_cwd_relative_data_path_join_form_in_mcp_servers():
    offenders = []
    for path in _iter_mcp_servers_py_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _RE_JOIN_FORM.search(line):
                rel = path.relative_to(_ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Found a pathlib join-form cwd-relative data path in mcp_servers/ "
        "(route it through core.constants.DATA_DIR instead):\n" + "\n".join(offenders)
    )


def _iter_runtime_py_files():
    for target in _RUNTIME_TARGETS:
        p = _ROOT / target
        if p.is_file() and p.suffix == ".py":
            yield p
        elif p.is_dir():
            yield from p.rglob("*.py")


def test_no_cwd_relative_data_paths():
    offenders = []
    for path in _iter_runtime_py_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(rx.search(line) for rx in _BANNED):
                rel = path.relative_to(_ROOT).as_posix()
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Found cwd-relative data paths (route these through "
        "core.constants.DATA_DIR instead):\n" + "\n".join(offenders)
    )
