"""POST /api/brain/ingest — write scraped .md into the vault via format_note.

Accepts JSON (one note or a list) or multipart file uploads. can_manage_brain
gated (when an auth_manager is provided); writes are path-safe and never clobber
an existing slug (collisions get a -2/-3 suffix).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.brain_ingest import format_note, KNOWN_CATEGORIES

SESSION_COOKIE = "prisma_session"


def _vault_root() -> str:
    # An explicit PRISMA_BRAIN_VAULT env var always wins when set — an empty
    # value deliberately disables ingest. This keeps tests and containerized
    # deployments hermetic even when a brain_vault_path setting exists.
    env = os.getenv("PRISMA_BRAIN_VAULT")
    if env is not None:
        return env
    # Legacy single-vault setting (back-compat).
    try:
        from src.settings import get_setting
        v = get_setting("brain_vault_path", "") or ""
        if v:
            return v
    except Exception:
        pass
    # Modular brains: write into the first discovered brain folder.
    try:
        from src.brains import discover_brains
        brains = discover_brains()
        if brains:
            return brains[0]["path"]
    except Exception:
        pass
    return ""


class IngestItem(BaseModel):
    filename: str = ""
    content: str
    category: Optional[str] = None
    source: Optional[str] = None


def _unique_path(folder: Path, slug: str) -> tuple[Path, str, str]:
    """Return (path, final_slug, status) — suffix the slug if it already exists."""
    cand, n = slug, 1
    status = "created"
    while (folder / (cand + ".md")).exists():
        n += 1
        cand = f"{slug}-{n}"
        status = "renamed"
    return folder / (cand + ".md"), cand, status


def _write_one(vault: Path, content: str, filename: str,
               category: Optional[str], source: Optional[str]) -> dict:
    cat, slug, md = format_note(content, filename, category=category, source=source)
    if cat not in KNOWN_CATEGORIES:
        cat = "imported"
    if not re.fullmatch(r"[a-z0-9-]{1,200}", slug):
        return {"status": "error", "error": "bad slug", "slug": slug, "category": cat}
    folder = (vault / "knowledge" / cat)
    folder.mkdir(parents=True, exist_ok=True)
    path, final_slug, status = _unique_path(folder, slug)
    # Bound the replace to the first (frontmatter) occurrence so a body that
    # happens to contain "slug: <value>" isn't rewritten on a rename.
    path.write_text(md.replace(f"slug: {slug}", f"slug: {final_slug}", 1), encoding="utf-8")
    return {"status": status, "slug": final_slug, "category": cat, "path": str(path)}


def setup_brain_ingest_routes(auth_manager=None) -> APIRouter:
    router = APIRouter(tags=["brain-ingest"])

    def _gate(request: Request) -> Optional[dict]:
        """Return an error dict if the caller may not manage the brain, else None."""
        if auth_manager is None:
            return None  # tests / AUTH disabled
        try:
            st = getattr(request, "state", None)
            if getattr(st, "api_token", False):
                # Bearer-token caller (the scraper): the real owner is in
                # api_token_owner; request.state.current_user is the literal
                # "api" sentinel, which has no privileges. (See app.py + the
                # codex_routes/webhook_routes pattern.)
                user = getattr(st, "api_token_owner", None)
            else:
                user = auth_manager.get_username_for_token(request.cookies.get(SESSION_COOKIE))
                if not user:
                    user = getattr(st, "current_user", None)
            if not user:
                return {"error": "not authenticated"}
            privs = auth_manager.get_privileges(user) or {}
            if not privs.get("can_manage_brain"):
                return {"error": "forbidden: can_manage_brain required"}
        except Exception:
            return {"error": "not authenticated"}
        return None

    @router.post("/api/brain/ingest")
    async def ingest(request: Request):
        gate = _gate(request)
        if gate:
            return gate
        vp = _vault_root()
        if not vp:
            return {"error": "no brain vault configured"}
        vault = Path(vp)
        results: list[dict] = []

        ctype = request.headers.get("content-type", "")
        if "multipart/form-data" in ctype:
            form = await request.form()
            for f in form.getlist("files"):
                raw = (await f.read()).decode("utf-8", "replace")
                name = getattr(f, "filename", "") or "upload.md"
                results.append(_write_one(vault, raw, name, None, "upload"))
            return {"results": results}

        body = await request.json()
        items = body if isinstance(body, list) else [body]
        for it in items:
            try:
                m = IngestItem(**it)
            except Exception as e:
                results.append({"status": "error", "error": f"bad item: {e}"})
                continue
            results.append(_write_one(vault, m.content, m.filename, m.category, m.source))
        return {"results": results}

    return router
