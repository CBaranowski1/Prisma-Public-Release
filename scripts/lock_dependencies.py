#!/usr/bin/env python3
"""Regenerate deterministic production and development requirement locks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UV = Path(sys.executable).with_name("uv.exe" if sys.platform == "win32" else "uv")


def _compile(source: str, output: str) -> None:
    subprocess.run(
        [
            str(UV),
            "--quiet",
            "pip",
            "compile",
            "--universal",
            "--python-version",
            "3.11",
            "--generate-hashes",
            "--output-file",
            output,
            source,
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> int:
    if not UV.is_file():
        print("Install the lock tool first: python -m pip install uv==0.12.0")
        return 2
    _compile("requirements.in", "requirements.txt")
    _compile("requirements-dev.in", "requirements-dev.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
