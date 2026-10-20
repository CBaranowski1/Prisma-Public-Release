"""Supply-chain checks for production/development dependency separation."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _package_lines(path: Path):
    return [
        line.strip()
        for line in path.read_text("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-", "\\"))
    ]


def test_direct_production_dependencies_are_exactly_pinned():
    for line in _package_lines(ROOT / "requirements.in"):
        assert re.search(r"(?<![<>=!~])==(?!=)", line), line


def test_test_and_evaluation_tools_are_not_in_production_lock():
    production = (ROOT / "requirements.txt").read_text("utf-8").lower()
    for package in ("pytest==", "pytest-asyncio==", "evalplus==", "datasets=="):
        assert not any(
            line.lower().startswith(package)
            for line in production.splitlines()
        )


def test_locked_packages_carry_hashes():
    production = (ROOT / "requirements.txt").read_text("utf-8")
    assert "--hash=sha256:" in production
    for line in _package_lines(ROOT / "requirements.txt"):
        assert "==" in line
