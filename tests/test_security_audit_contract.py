"""Keep security-audit suppressions narrow and machine-verifiable."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDITED_TREES = ("src", "core", "routes", "scripts")


def test_nosec_annotations_name_specific_bandit_checks():
    generic = []
    for tree in AUDITED_TREES:
        for path in (ROOT / tree).rglob("*.py"):
            for lineno, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                if "# nosec" in line and not re.search(
                    r"# nosec B\d{3}(?:\s|$)",
                    line,
                ):
                    generic.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not generic, f"generic security suppressions: {generic}"


def test_bandit_is_a_locked_development_gate():
    dev_input = (ROOT / "requirements-dev.in").read_text("utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "bandit==1.9.4" in dev_input
    assert "python -m bandit -q -r src core routes scripts -ll" in workflow
