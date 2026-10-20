import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_dispatcher():
    path = Path(__file__).resolve().parent.parent / "scripts" / "odysseus"
    loader = importlib.machinery.SourceFileLoader("prisma_dispatcher_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file mode bits not meaningful on Windows",
)
def test_is_runnable_subcommand_requires_executable_file(tmp_path):
    cli = _load_dispatcher()
    sub = tmp_path / "prisma-demo"
    sub.write_text("#!/bin/sh\n")
    sub.chmod(0o644)

    assert cli._is_runnable_subcommand(sub) is False

    sub.chmod(0o755)
    assert cli._is_runnable_subcommand(sub) is True
