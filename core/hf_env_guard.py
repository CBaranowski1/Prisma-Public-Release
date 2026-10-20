"""Consolidated HuggingFace symlink workaround.

On a network-share / UNC data dir, Windows can't follow HuggingFace hub's
symlinks ([WinError 1463] "symbolic link cannot be followed because its type
is disabled"), so the ONNX embedding model fails to load. huggingface_hub
reads HF_HUB_DISABLE_SYMLINKS at import time, so it must be set before
huggingface_hub/fastembed is first imported anywhere in the process.

This was previously duplicated line-for-line (and already drifting in
comment text) between app.py and src/embeddings.py, and was gated on
``os.name == "nt"`` only. Non-Windows operators on network-mounted data dirs
(NFS/CIFS/SMB) hit the exact same symlink problem, so this helper detects
"is this path network-backed" cross-OS and force-sets the guard whenever it
is, regardless of OS.

Known limitation: macOS has no ``/proc/mounts`` equivalent probed here, so
network-mounted data dirs on macOS are not detected (fails safe: the guard
simply doesn't fire, matching today's pre-existing non-Windows behavior).

Keep this module stdlib-only and dependency-light — it must be safe to call
at process bootstrap, before any heavy app module (huggingface_hub,
fastembed, or any other third-party dependency) is imported.
"""

import os
import sys

# Linux/POSIX filesystem types considered network-backed for symlink purposes.
_NETWORK_FSTYPES = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "afs"}


def _default_data_dir() -> str:
    """Resolve the same PRISM_DATA_DIR override that core/constants.py uses,
    without importing core.constants (keeps this module dependency-light and
    avoids pulling in anything heavier at bootstrap time)."""
    override = os.getenv("PRISM_DATA_DIR")
    if override and override.strip():
        return override.strip()
    return os.getenv("DATA_DIR", "")


def _is_network_path(path: str) -> bool:
    """Best-effort, cross-OS network/remote-filesystem detection. Never
    raises — any detection failure is treated as 'not network' (fail safe:
    the guard simply doesn't fire, matching today's non-Windows behavior
    when detection is inconclusive)."""
    try:
        if not path:
            return False
        if sys.platform == "win32":
            # UNC path: \\server\share\...
            if path.startswith("\\\\") or path.startswith("//"):
                return True
            # Mapped network drive (Z:\): GetDriveTypeW == DRIVE_REMOTE (4)
            drive = os.path.splitdrive(path)[0]
            if drive:
                import ctypes

                DRIVE_REMOTE = 4
                return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE
            return False
        # POSIX: match the longest /proc/mounts entry (Linux only; macOS has
        # no /proc — falls through to "not network", a known, documented
        # limitation, see module docstring).
        if os.path.exists("/proc/mounts"):
            resolved = os.path.realpath(path)
            best_match, best_fstype = "", ""
            with open("/proc/mounts", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mnt_point, fstype = parts[1], parts[2]
                    if resolved.startswith(mnt_point) and len(mnt_point) > len(best_match):
                        best_match, best_fstype = mnt_point, fstype
            return best_fstype in _NETWORK_FSTYPES
        return False
    except Exception:
        return False


def ensure_hf_symlinks_disabled_if_networked(path: str | None = None) -> None:
    """Idempotent; safe to call multiple times; must run before the first
    huggingface_hub/fastembed import anywhere in the process.

    When ``path`` (or the resolved PRISM_DATA_DIR/DATA_DIR default) is
    network-detected, force HuggingFace hub to copy model files instead of
    symlinking them, cross-OS (not gated on os.name == "nt"). Uses
    ``setdefault`` so an operator's own explicit env var value is never
    overridden.
    """
    if _is_network_path(path if path is not None else _default_data_dir()):
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
