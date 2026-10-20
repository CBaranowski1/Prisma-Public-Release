"""Atomic JSON file writes.

Use this everywhere a JSON config file is persisted. A plain `open("w") +
json.dump` truncates the file on first write and only fills it with new
content afterwards — a kill -9 / power loss / OOM in between produces a
truncated or empty file. For password DBs (`auth.json`) and live state
(`sessions.json`, `settings.json`, `integrations.json`, `cookbook_state.json`),
that's a data-loss event.

`atomic_write_json` writes to a sibling tmp file, fsyncs, then `os.replace`s
into place. On POSIX `os.replace` is atomic on the same filesystem.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional


def atomic_write_json(path: str, data: Any, *, indent: Optional[int] = None) -> None:
    """Atomically persist `data` as JSON at `path`.

    The temp file uses the live PID as a suffix so two processes saving the
    same file (e.g. unit tests) don't collide on the rename target.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_npy(path: str, arr) -> None:
    """Atomically persist a numpy array as `.npy` at `path` (GRAY-BRN-07).

    Mirrors `atomic_write_json`'s temp-file + fsync + os.replace shape exactly,
    with one critical difference: the temp file MUST be opened as a binary
    file OBJECT and that object (not a path string) passed to `np.save`.
    `numpy.save` silently appends a `.npy` suffix to any string target that
    doesn't already end with it — which would rewrite the pid-suffixed temp
    path (`{path}.tmp.{pid}`) out from under the subsequent `os.replace` call,
    either raising FileNotFoundError or leaving a stray suffixed temp file
    behind. Passing an already-open file object bypasses that auto-suffix
    behavior entirely, so the temp-file naming scheme (and the atomicity it
    provides) stays intact. numpy is imported locally to keep this module
    import-light for callers that never touch `.npy` files.
    """
    import numpy as np

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        np.save(f, arr)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
