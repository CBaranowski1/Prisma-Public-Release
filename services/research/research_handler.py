# services/research/research_handler.py
"""Compatibility wrapper for the canonical src.research_handler module.

``services.research.research_handler`` remains importable for older service
code and tests, but the implementation now lives in ``src.research_handler``
(the copy wired into app_initializer and task_scheduler) so background-task
handling, timeout logic, and report formatting cannot drift between copies.
"""

import sys

from src import research_handler as _research_handler

# Mirror the live module's names here so loaders that keep a direct reference
# to this module object (e.g. importlib.util.spec_from_file_location in tests)
# see the same API as normal imports.
globals().update(
    {k: v for k, v in vars(_research_handler).items()
     if not (k.startswith("__") and k.endswith("__"))}
)

# Normal imports resolve straight to the live module.
sys.modules[__name__] = _research_handler
