"""Modular brain discovery.

A `brains/` directory where each subfolder is a self-contained brain:
    brains/<id>/brain.json            (manifest: id?, name, color, description?, enabled?)
    brains/<id>/knowledge/<cat>/*.md  (notes)
    brains/<id>/config/...            (optional — this brain's council agents)

Single source of truth for: where brains live (`brains_root`), what brains exist
(`discover_brains`), resolving an id -> folder (`resolve_brain`), and building the
per-brain + combined providers (`build_brain_providers`). Routes + the app
initializer import from here so the discovery rules live in one place.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, List, Dict

try:
    from src.constants import BASE_DIR as _BASE_DIR
except Exception:  # pragma: no cover
    _BASE_DIR = str(Path(__file__).resolve().parent.parent)

_ID_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(s: str) -> str:
    return _ID_RE.sub("-", (s or "").strip().lower()).strip("-") or "brain"


def brains_root() -> Path:
    """Where brain folders live: setting `brains_root` -> env PRISMA_BRAINS_ROOT
    -> <repo>/brains (the in-repo, shipped-with-Prisma default)."""
    try:
        from src.settings import get_setting
        v = (get_setting("brains_root", "") or "").strip()
        if v:
            return Path(v)
    except Exception:
        pass
    env = os.getenv("PRISMA_BRAINS_ROOT", "").strip()
    if env:
        return Path(env)
    return Path(_BASE_DIR) / "brains"


def _legacy_vault_path() -> str:
    try:
        from src.settings import get_setting
        v = (get_setting("brain_vault_path", "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return (os.getenv("PRISMA_BRAIN_VAULT", "") or "").strip()


def _read_manifest(folder: Path) -> Dict:
    f = folder / "brain.json"
    if f.is_file():
        try:
            return json.loads(f.read_text("utf-8")) or {}
        except Exception:
            return {}
    return {}


def _brain_from_folder(folder: Path) -> Optional[Dict]:
    """A folder is a brain if it has knowledge/ or a brain.json. Disabled brains
    (manifest enabled:false) are skipped.

    `path` = the KNOWLEDGE root (where knowledge/<cat>/*.md + config/ live). It is
    the folder itself, unless the manifest sets `vault_path` (absolute, or
    relative to the folder) to point at an external vault — that lets a small
    in-repo brain folder reference a big external knowledge base.
    `dir` = the brain folder itself (holds brain.json + a baked graph.json)."""
    if not folder.is_dir():
        return None
    m = _read_manifest(folder)
    vp = (m.get("vault_path") or "").strip()
    if vp:
        p = Path(vp)
        path = p if p.is_absolute() else (folder / p)
        try:
            path = path.resolve()
        except Exception:
            pass
        # Visibility (non-breaking): flag a manifest vault_path that resolves
        # outside the brains root. We do NOT block it — external vaults are a
        # supported feature — but a surprising path is worth a log line.
        try:
            import logging as _lg
            _root = brains_root().resolve()
            if _root not in path.parents and path != _root:
                _lg.getLogger("brains").info("brain %s vault_path resolves outside brains root: %s", folder.name, path)
        except Exception:
            pass
    else:
        path = folder
    if not (path / "knowledge").is_dir() and not m:
        return None
    if m.get("enabled") is False:
        return None
    bid = _slug(m.get("id") or folder.name)
    name = m.get("name") or folder.name.replace("-", " ").replace("_", " ").title()
    return {
        "id": bid,
        "name": name,
        "color": (m.get("color") or "").strip(),
        "description": (m.get("description") or "").strip(),
        "path": str(path),
        "dir": str(folder),
    }


def discover_brains(root: Optional[Path] = None) -> List[Dict]:
    """All brains under `root` (default brains_root()), plus a legacy bridge that
    surfaces a configured `brain_vault_path` as one brain when it lives outside
    brains_root (so the pre-modular single-vault setup keeps working)."""
    root = Path(root) if root else brains_root()
    out: List[Dict] = []
    seen: set = set()
    if root.is_dir():
        for child in sorted(root.iterdir()):
            b = _brain_from_folder(child)
            if b and b["id"] not in seen:
                seen.add(b["id"])
                out.append(b)
    legacy = _legacy_vault_path()
    if legacy:
        lp = Path(legacy)
        try:
            inside = (lp.resolve() == root.resolve()) or (root.resolve() in lp.resolve().parents)
        except Exception:
            inside = False
        if not inside and (lp / "knowledge").is_dir() and "legacy" not in seen:
            out.append({
                "id": "legacy", "name": "Legacy Vault", "color": "",
                "description": "Vault from brain_vault_path.", "path": str(lp), "dir": str(lp),
            })
    return out


def resolve_brain(bid: str, root: Optional[Path] = None) -> Optional[Dict]:
    for b in discover_brains(root):
        if b["id"] == bid:
            return b
    return None


_VAULT_CLS = None


def _vault_provider_class():
    """Load PrecisionVaultProvider from providers/precision_vault.py (the same
    file discover_providers loads). Cached. Kept out of normal import since
    providers/ is not a package."""
    global _VAULT_CLS
    if _VAULT_CLS is not None:
        return _VAULT_CLS
    import importlib.util
    p = Path(_BASE_DIR) / "providers" / "precision_vault.py"
    spec = importlib.util.spec_from_file_location("prisma_brain_vault_provider", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    _VAULT_CLS = mod.PrecisionVaultProvider
    return _VAULT_CLS


def build_brain_providers() -> List:
    """Return [GuardedProvider(per-brain) ..., CombinedBrainProvider] for every
    discovered brain. Empty list when there are no brains."""
    from src.brain_provider import GuardedProvider, CombinedBrainProvider
    VaultProvider = _vault_provider_class()
    per = []
    for b in discover_brains():
        try:
            inner = VaultProvider(b["path"], provider_id="brain:" + b["id"],
                                  display_name=b["name"], color=b.get("color"),
                                  index_dir=b.get("dir") or b["path"])
            per.append(GuardedProvider(inner))
        except Exception:
            continue
    out = list(per)
    if per:
        out.append(CombinedBrainProvider(per))
    return out
