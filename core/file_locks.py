"""Per-canonical-path lock registry (GRAY-BRN-08).

Generalizes this codebase's established per-instance `threading.RLock` idiom
(`core/auth.py::AuthManager._config_lock`, `src/api_key_manager.py::
APIKeyManager._lock`) to a free-function, path-keyed registry. That idiom
guards a single manager instance's read-modify-write critical section;
`append_learned` (src/brain_index.py) is a module-level function operating on
an arbitrary `index_dir`, so instead of one lock per class instance, this
module hands out one lock per resolved filesystem path -- callers writing to
the SAME canonical path (however it's spelled: relative, absolute, with
`..` segments, etc.) always contend on the SAME lock object, while writers to
DIFFERENT paths never block each other.

This registry is process-local (in-process only): it does not, and cannot,
serialize a separate OS process (e.g. the MCP `memory_server` subprocess)
writing to the same file. See 30-RESEARCH.md Pitfall 4 / Open Question 1 for
the documented scope of that residual gap.
"""

from __future__ import annotations

import threading
from pathlib import Path

# Bootstrap lock: guards creation of new entries in `_locks` so two threads
# never race to create two different RLock objects for the same canonical
# path. Once an entry exists, callers use the RLock it maps to, not this one.
_registry_lock = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def get_lock(path) -> threading.RLock:
    """Return the process-wide RLock for `path`'s canonical (resolved) form.

    Same path -- however spelled (relative, absolute, with `..` segments) --
    always maps to the same lock object. Distinct paths get distinct locks.
    Returns a `threading.RLock` (re-entrant), matching `core/auth.py` and
    `src/api_key_manager.py`'s stated rationale: a guarded method may need to
    call another guarded path keyed on the same canonical path without
    deadlocking the calling thread.
    """
    key = str(Path(path).resolve())
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock
