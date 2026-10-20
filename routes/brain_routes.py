"""Brain provider API: list providers, search, categories, and config."""
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from typing import Optional, Dict, List

from src.auth_helpers import require_privilege


class BrainConfigUpdate(BaseModel):
    vault_path: Optional[str] = None
    enabled: Optional[Dict[str, bool]] = None
    # Learning loop (admin): master switch + per-user opt-out list.
    learning_enabled: Optional[bool] = None
    learning_optout: Optional[List[str]] = None


def setup_brain_routes(registry):
    router = APIRouter(prefix="/api/brain", tags=["brain"])

    def _get(provider_id):
        try:
            return registry.get(provider_id)
        except KeyError:
            raise HTTPException(404, f"unknown brain provider: {provider_id}")

    @router.get("/providers")
    async def providers():
        return [{"provider_id": p.provider_id, "display_name": p.display_name,
                 "enabled": getattr(p, "enabled", True),
                 "healthy": getattr(p, "healthy", True)} for p in registry.all()]

    @router.get("/categories")
    async def categories(provider: str = Query(...)):
        items = list(await _get(provider).categories())
        if provider in {"brain:vera-brain", "brain:all"} and "vera-agents" not in items:
            items.append("vera-agents")
        return sorted(items)

    @router.get("/search")
    async def search(q: str = Query(""), provider: str = Query(...),
                     category: str | None = Query(None)):
        docs = await _get(provider).search(q, category=category)
        rows = [d.__dict__ for d in docs]
        if provider in {"brain:vera-brain", "brain:all"} and category in {None, "vera-agents"}:
            from routes.vera_agents_routes import catalog_documents

            rows.extend(catalog_documents(q, limit=20))
        if (q or "").strip():
            rows.sort(key=lambda row: float(row.get("score") or 0), reverse=True)
        return rows

    @router.get("/config")
    async def get_config(request: Request):
        # SECURITY (audit P3): brain config exposes the vault filesystem path and
        # the per-user learning opt-out list. Restrict to can_manage_brain.
        require_privilege(request, "can_manage_brain", fail_closed=True)
        try:
            from src.settings import load_settings
            s = load_settings()
            return {
                "vault_path": s.get("brain_vault_path", ""),
                "enabled": s.get("brain_enabled", {}),
                "learning_enabled": s.get("brain_learning_enabled", True),
                "learning_optout": s.get("brain_learning_optout", []),
            }
        except Exception:
            return {"vault_path": "", "enabled": {},
                    "learning_enabled": True, "learning_optout": []}

    @router.put("/config")
    async def put_config(body: BrainConfigUpdate, request: Request):
        # SECURITY (audit P3): this mutates GLOBAL brain settings (vault path,
        # provider enabled flags, learning switch, opt-out users). Previously
        # any authenticated user could call it; require can_manage_brain.
        require_privilege(request, "can_manage_brain", fail_closed=True)
        try:
            from src.settings import load_settings, save_settings
            s = load_settings()
            if body.vault_path is not None:
                s["brain_vault_path"] = body.vault_path
            if body.enabled is not None:
                existing = s.get("brain_enabled", {})
                existing.update(body.enabled)
                s["brain_enabled"] = existing
            if body.learning_enabled is not None:
                s["brain_learning_enabled"] = bool(body.learning_enabled)
            if body.learning_optout is not None:
                # De-duplicate, drop blanks, keep stable order.
                seen, clean = set(), []
                for u in body.learning_optout:
                    u = (u or "").strip()
                    if u and u not in seen:
                        seen.add(u); clean.append(u)
                s["brain_learning_optout"] = clean
            save_settings(s)
            return {
                "vault_path": s.get("brain_vault_path", ""),
                "enabled": s.get("brain_enabled", {}),
                "learning_enabled": s.get("brain_learning_enabled", True),
                "learning_optout": s.get("brain_learning_optout", []),
            }
        except Exception as exc:
            raise HTTPException(500, f"Failed to save brain config: {exc}")

    return router
