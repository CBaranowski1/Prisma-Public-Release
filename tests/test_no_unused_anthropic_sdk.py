"""Regression guard for GRAY-INT-06 (Phase 25): `@anthropic-ai/sdk` was
confirmed unused in all runtime source and dropped from `package.json` --
the server calls every LLM provider via `httpx` in `src/llm_core.py`. This
test fails loudly if the dependency or an import of it is ever silently
re-added, so the "3 disagreeing versions" confusion cannot recur.
"""
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Built from parts so the literal string isn't a maintenance/grep magnet.
_SDK_PACKAGE = "@" + "anthropic-ai" + "/" + "sdk"

_SKIP_DIRS = {"node_modules", ".git", ".planning", "tests", "venv", ".venv", "data"}
_SOURCE_EXTS = {".py", ".js", ".ts"}
_SKIP_FILES = {"package.json", "package-lock.json"}


def _iter_source_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _SOURCE_EXTS:
            continue
        if path.name in _SKIP_FILES:
            continue
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        yield path


def test_sdk_absent_from_package_json():
    package_json = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert _SDK_PACKAGE not in (package_json.get("dependencies") or {})


def test_no_runtime_import_of_sdk():
    needles = [
        f"from '{_SDK_PACKAGE}'",
        f'from "{_SDK_PACKAGE}"',
        f"require('{_SDK_PACKAGE}')",
        f'require("{_SDK_PACKAGE}")',
        "new Anthropic(",
    ]
    offenders = []
    for path in _iter_source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(needle in text for needle in needles):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"Found @anthropic-ai/sdk usage in: {offenders}"
