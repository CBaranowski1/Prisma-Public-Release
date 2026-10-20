"""Regression checks for the sanitized public GitHub checkout.

No credential values are emitted by these tests.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_portable_sparklab_overlay_has_no_personal_mounts():
    text = (ROOT / "docker" / "sparklab-glm.yml").read_text(encoding="utf-8")
    assert "/home/sparky/" not in text
    assert "/Users/" not in text
    assert "C:\\Users\\" not in text
    assert "172.18.0.1" not in text
    assert "DGX_SPARK_ALLOW_INSECURE_NO_AUTH: \"false\"" in text
    assert "DGX_SPARK_API_KEY: ${DGX_SPARK_API_KEY:-}" in text


def test_gitignore_does_not_hide_committed_sensitive_files():
    if not (ROOT / ".git").exists():
        return
    result = subprocess.run(
        ["git", "ls-files", "-ci", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert not result.stdout.strip(), "Tracked files match .gitignore; audit them before release"
