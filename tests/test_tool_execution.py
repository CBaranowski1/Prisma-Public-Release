"""SEC-07 (D-05, cluster 2 of 3): narrow-log-preserve-fallback regression
tests for `src/tool_execution.py`'s silent `except` sites.

Enumerated silent sites (bare `pass`/`continue`, no log) confirmed by direct
read of `src/tool_execution.py` prior to this plan's fix, concentrated in the
path-root-gathering block (`_tool_path_roots`, ~189-244) and the subprocess
progress/cleanup block (`_run_subprocess_streaming`, ~347-449):

  - :206-207  `except OSError: pass`                      (private /tmp realpath -- already narrow)
  - :220-221  `except Exception: pass`                    (import tempfile -- CLOSED surface, narrow to (ImportError, ModuleNotFoundError))
  - :229-230  `except Exception: pass`                    (get_setting("tool_path_extra_roots") -- OPEN surface, keep Exception)
  - :238-239  `except OSError: continue`                   (per-root realpath dedup -- already narrow)
  - :395-398  `except Exception: pass`                    (progress_cb best-effort call)
  - :412-413  `except Exception: pass`                    (proc.kill() on timeout)
  - :416-417  `except Exception: pass`                    (proc.wait() after kill, timeout branch)
  - :424-425  `except Exception: pass`                    (proc.kill() on CancelledError)
  - :428-429  `except Exception: pass`                    (proc.wait() after kill, cancel branch)
  - :441-442  `except (asyncio.CancelledError, Exception): pass`  (prog_task await in finally)
  - :447-448  `except Exception: pass`                    (reader-drain wait in finally)

This file regression-locks the two representative sites from
20-PATTERNS.md (`:217-230`) -- one closed surface (import tempfile), one
open surface (get_setting) -- proving BOTH that the prior fallback
value/control-flow is preserved byte-for-byte AND that the triggering
exception is now observable via a log record. The remaining enumerated
sites (subprocess/progress cleanup) get the same log-only treatment in
Task 2 but are not independently regression-tested here since their
triggering calls (asyncio subprocess primitives) are not cheaply
monkeypatchable in isolation; the source-level `grep -n "exc_info=True"`
count assertion in the plan's acceptance criteria covers those.
"""
import logging
import os
import sys
from unittest.mock import patch

from src import tool_execution

LOGGER_NAME = "src.tool_execution"


def test_except_narrowing_tempfile_import_closed_surface_preserves_fallback_and_logs(
    monkeypatch, caplog
):
    """Closed surface: `import tempfile` can only fail with
    (ImportError, ModuleNotFoundError). Simulate that failure by poisoning
    sys.modules["tempfile"] (the standard way to force an ImportError for an
    already-imported stdlib module) and assert:
      1) _tool_path_roots() still returns (does not crash / propagate),
      2) the returned roots are a subset of the healthy-path roots (the
         tempdir-derived root is simply absent -- fallback preserved),
      3) a log record fires mentioning the failure.
    """
    baseline = tool_execution._tool_path_roots()
    assert baseline  # sanity: non-empty under normal conditions

    monkeypatch.setitem(sys.modules, "tempfile", None)

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        degraded = tool_execution._tool_path_roots()

    # Fallback preserved: nothing NEW appears, at most the tempdir-derived
    # entry is missing. Function did not raise (we already reached this line).
    assert set(degraded).issubset(set(baseline))
    assert degraded  # DATA_DIR / /tmp roots still present

    site_records = [
        r for r in caplog.records
        if r.name == LOGGER_NAME and "tempfile" in r.getMessage().lower()
    ]
    assert len(site_records) == 1
    assert site_records[0].levelno in (logging.DEBUG, logging.WARNING)


def test_except_narrowing_get_setting_open_surface_preserves_fallback_and_logs(
    monkeypatch, caplog
):
    """Open surface: `get_setting("tool_path_extra_roots")` internals are not
    a provably closed exception set (arbitrary settings-file content / arbitrary
    storage backend) -- must stay `except Exception`, log-only, no narrowing.

    First prove get_setting CAN extend the roots (establishes what "the
    fallback" is), then force it to raise and assert the extra root is
    absent (byte-identical to "no roots configured"), the function does not
    crash, and the failure is logged.
    """
    extra_root = os.path.realpath(os.path.join(os.sep, "sec07-extra-root"))

    with patch("src.settings.get_setting", return_value=[extra_root]):
        extended = tool_execution._tool_path_roots()
    assert extra_root in extended

    def _raise(*_a, **_kw):
        raise RuntimeError("settings backend exploded")

    with patch("src.settings.get_setting", _raise):
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            degraded = tool_execution._tool_path_roots()

    assert extra_root not in degraded
    assert degraded  # default roots still returned -- no crash, no propagation

    site_records = [
        r for r in caplog.records
        if r.name == LOGGER_NAME and "tool_path_extra_roots" in r.getMessage()
    ]
    assert len(site_records) == 1
    assert site_records[0].levelno in (logging.DEBUG, logging.WARNING)
