"""Tests for the neutral, council-agnostic curation module (src/context_curation.py).

Proves GUARD-01's no-council-coupling contract at the import-graph level (not just
a runtime smoke import — a plain `import` would still succeed even if the module
imported something unused from src.council), and smoke-checks the relocated
primitives behave identically to their original fresh_context.py definitions.
"""
import ast
import inspect

import src.context_curation as cc
from src.context_curation import curate, distill, fits, CuratedList


def test_no_council_import():
    """src/context_curation.py must not import anything from src.council —
    that coupling is exactly what GUARD-01 requires the module to be free of."""
    source = inspect.getsource(cc)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("src.council"), (
                f"src/context_curation.py imports from src.council: {node.module}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("src.council"), (
                    f"src/context_curation.py imports from src.council: {alias.name}"
                )


def test_primitives_reexported():
    empty = curate([])
    assert isinstance(empty, CuratedList)
    assert empty == []
    assert empty.dropped_count == 0

    assert distill("x", 0) == ""              # never-raises cap<=0 guard

    assert fits("a" * 10, 5) is False
    assert fits("a" * 3, 5) is True
