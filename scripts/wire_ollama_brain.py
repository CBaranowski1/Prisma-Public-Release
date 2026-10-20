"""Wire the prism application to local Ollama + enable the brain. Idempotent.

Run from the prism-UI repo root with its venv:
    .\venv\Scripts\python.exe scripts\wire_ollama_brain.py

- Registers (or reuses) a 'Local Ollama' model endpoint at the OpenAI-compatible
  Ollama URL (http://127.0.0.1:11434/v1) and makes it the default endpoint.
- Pins the sharpened VERA models so they show without probing.
- Enables the llm-council + precision-audit brains for retrieval.
- Runs a brain recall smoke test and prints a summary.
"""
from __future__ import annotations
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OLLAMA_URL = os.getenv("OLLAMA_OPENAI_URL", "http://127.0.0.1:11434/v1")
PINNED = ["vera-coder", "vera-reasoner", "vera-fast", "vera-security", "vera-tester",
          "qwen2.5-coder:7b", "qwen3:14b", "llama3.1:8b"]


def main() -> int:
    from core.database import SessionLocal, ModelEndpoint
    from src.settings import load_settings, save_settings

    db = SessionLocal()
    try:
        ep = (db.query(ModelEndpoint)
                .filter(ModelEndpoint.base_url == OLLAMA_URL)
                .first())
        if ep is None:
            ep = ModelEndpoint(
                id=uuid.uuid4().hex, name="Local Ollama", base_url=OLLAMA_URL,
                is_enabled=True, endpoint_kind="local", model_type="llm",
                owner=None, pinned_models=json.dumps(PINNED),
            )
            db.add(ep)
            action = "created"
        else:
            ep.is_enabled = True
            ep.endpoint_kind = "local"
            ep.pinned_models = json.dumps(PINNED)
            action = "updated"
        db.commit()
        print(f"[ok] Ollama endpoint {action}: {ep.id}  ({OLLAMA_URL})")
    finally:
        db.close()

    s = load_settings()
    s["default_endpoint_id"] = ep.id
    be = s.get("brain_enabled") or {}
    be["llm-council"] = True
    be["precision-audit"] = True
    s["brain_enabled"] = be
    save_settings(s)
    print(f"[ok] default_endpoint_id set; brains enabled: {list(be)}")

    # brain recall smoke
    try:
        from providers.precision_vault import PrecisionVaultProvider  # noqa
    except Exception:
        pass
    try:
        from src.brains import discover_brains
        brains = discover_brains()
        print(f"[ok] brains discovered: {[b['id'] for b in brains]}")
    except Exception as e:
        print(f"[warn] brain discovery: {e}")
    print("\nDone. Restart the app (or it picks this up on next start).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
