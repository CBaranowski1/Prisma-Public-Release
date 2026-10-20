import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPERS = [
    ROOT / "integrations" / "codex" / "scripts" / "odysseus_api.py",
    ROOT
    / "integrations"
    / "claude"
    / "skills"
    / "odysseus"
    / "scripts"
    / "odysseus_api.py",
]


def _load(path):
    name = f"odysseus_api_{path.parent.parent.name}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", HELPERS)
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://prisma.internal",
        "https://user:secret@prisma.internal",
        "https://prisma.internal?token=secret",
        "https://prisma.internal/#fragment",
        r"https://prisma.internal\@attacker.example",
    ],
)
def test_helpers_reject_non_http_or_ambiguous_urls(path, url):
    with pytest.raises(ValueError):
        _load(path)._validate_base_url(url)


@pytest.mark.parametrize("path", HELPERS)
def test_helpers_preserve_valid_subpath_without_query(path):
    helper = _load(path)

    assert (
        helper._validate_base_url("HTTPS://prisma.internal:7443/root/")
        == "https://prisma.internal:7443/root"
    )
