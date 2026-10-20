#!/usr/bin/env python3
"""Conservative public-release preflight; does not replace a secret scanner.

Reports paths and pattern categories, never matching secret values.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

DENY_NAMES = {".env", "auth.json", "sessions.json", "credentials.json",
              "secrets.json", "prisma.db", "app.db"}
DENY_EXT = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3",
            ".db", ".kdbx"}
DENY_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules"}
PATTERNS = {
    "private-key-marker": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(rb"gh[pousr]_[A-Za-z0-9_]{30,}"),
    "slack-token": re.compile(rb"xox[baprs]-[A-Za-z0-9-]{15,}"),
    "openai-token": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{30,}"),
    "aws-access-key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
}
# Exact-content exemption for documented fake unit-test inputs. Any edit
# invalidates it; new credentials in this file are therefore still reported.
REVIEWED_SYNTHETIC = {
    "tests/test_brain_privacy.py": (
        "61cbbf046e8129e5d409d0d4953fc08de52835f3a30d8b5edfd535b76de96204",
        frozenset({"private-key-marker", "aws-access-key"}),
    )
}
MAX_SCAN = 3_000_000
# Public snapshots must not contain private development or specialist configuration.
PUBLIC_EXCLUDED_PREFIXES = (".planning/", ".claude/settings", "brains/precision-audit/")

def check(root: Path, paths: list[str], *, public_candidate: bool = False) -> list[str]:
    findings = []
    for name in paths:
        p = Path(name)
        if public_candidate and name.startswith(PUBLIC_EXCLUDED_PREFIXES):
            findings.append(f"{name}: private-release-category")
            continue
        if p.is_absolute() or ".." in p.parts:
            findings.append(f"{name}: invalid-path")
            continue
        if any(part in DENY_DIRS for part in p.parts):
            continue
        if p.name.lower() in DENY_NAMES or (
                p.name.startswith(".env") and p.name != ".env.example"):
            findings.append(f"{name}: sensitive-filename")
        if p.suffix.lower() in DENY_EXT:
            findings.append(f"{name}: sensitive-extension")
        full = root / p
        if full.is_symlink():
            findings.append(f"{name}: symlink-needs-review")
            continue
        if not full.is_file():
            continue
        if full.stat().st_size > MAX_SCAN:
            findings.append(f"{name}: large-file-needs-review")
            continue
        blob = full.read_bytes()
        if b"\0" in blob[:4096]:
            continue
        reviewed = REVIEWED_SYNTHETIC.get(name)
        allowed = (reviewed[1] if reviewed and
                   hashlib.sha256(blob).hexdigest() == reviewed[0] else frozenset())
        for category, pattern in PATTERNS.items():
            if category not in allowed and pattern.search(blob):
                findings.append(f"{name}: {category}")
    return findings

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--include-planning", action="store_true")
    parser.add_argument("--public-candidate", action="store_true",
                        help="Fail on internal development and specialist paths")
    a = parser.parse_args()
    root = a.root.resolve()
    try:
        tracked = subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "-z"]
        ).decode("utf-8").split("\0")
    except (subprocess.CalledProcessError, UnicodeError):
        print("Unable to inventory Git-tracked paths.", file=sys.stderr)
        return 2
    tracked = [p for p in tracked if p]
    if not a.include_planning and not a.public_candidate:
        tracked = [p for p in tracked if not p.startswith(".planning/")]
    findings = check(root, tracked, public_candidate=a.public_candidate)
    for item in findings:
        print(item)
    print(f"Reviewed {len(tracked)} tracked paths; {len(findings)} findings")
    return 1 if findings else 0

if __name__ == "__main__":
    raise SystemExit(main())
