"""Organize the VERA agents into purpose-built TEAMS (runnable councils).

Each team = a small council: specialists fan out, reviewers check, one chief
synthesizes. Writes config/teams/<id>.json (prism CouncilConfig shape). Load one
via the council config (PUT /api/council/config) to run that team.

Seats are wired to the local vera Ollama fleet: each seat gets a concrete
`model` (see MODEL_MAP) and the local Ollama `endpoint_id` (env
VERA_ENDPOINT_ID, else `default_endpoint_id` from data/settings.json).
"""
from __future__ import annotations
import json
import os
import glob
import yaml

# ── Local vera fleet (Ollama tags; see scripts/wire_ollama_brain.py) ──
# Role-type → concrete local model.
MODEL_MAP = {
    "triage": "vera-fast",        # llama3.1 8B — routing/classification
    "coder": "vera-coder",        # qwen2.5-coder 14B — codegen specialists
    "reasoner": "vera-reasoner",  # qwen3 14B — architecture/review/chiefs
    "security": "vera-security",  # 7B — security review
    "tester": "vera-tester",      # 7B — test authoring/review
    "flagship": "vera-30b",       # qwen3-coder 30B — final code synthesizer
}
# Logical model_policy.default (persona YAML) → concrete local model.
POLICY_MAP = {
    "local_fast": MODEL_MAP["triage"],
    "local_code": MODEL_MAP["coder"],
    "local_reasoning": MODEL_MAP["reasoner"],
    "stronger_local": MODEL_MAP["flagship"],
}


def _pick_model(cfg: dict) -> str:
    """Concrete vera model for one persona.

    Precedence: explicit concrete override in the persona's model_policy
    (a literal "vera-*" tag) > role-type mapping (triage/security/test/
    architecture/chief) > the persona's logical model_policy.default >
    coder-for-specialists fallback.
    """
    pid = cfg.get("id", "")
    role = cfg.get("role", "specialist")
    policy = ((cfg.get("model_policy") or {}).get("default") or "").strip()
    if policy.startswith("vera-"):          # per-persona concrete override
        return policy
    if role == "triage":
        return MODEL_MAP["triage"]
    if any(k in pid for k in ("security", "depsec")):
        return MODEL_MAP["security"]
    if any(k in pid for k in ("testing", "tester")):
        return MODEL_MAP["tester"]
    if "architecture" in pid or role in ("chief", "mid_overseer"):
        return MODEL_MAP["reasoner"]
    if policy in POLICY_MAP:                # persona's own logical policy
        return POLICY_MAP[policy]
    return MODEL_MAP["coder"] if role == "specialist" else MODEL_MAP["reasoner"]


# Team specs. Optional keys:
#   triage      — persona id emitted as a first layer every panelist consumes
#   synth_model — force the synthesizer's model (e.g. the flagship 30B)
TEAMS = {
    "codegen_team": {"name": "Codegen Team",
        "panelists": ["specialist_codegen", "specialist_backend", "specialist_architecture",
                      "reviewer_correctness", "reviewer_performance"],
        "synth": "specialist_chief", "synth_model": MODEL_MAP["flagship"]},
    "review_team": {"name": "Review Team",
        "panelists": ["reviewer_correctness", "reviewer_performance", "reviewer_security",
                      "reviewer_maintainability", "general_devils_advocate"],
        "synth": "overseer_chief"},
    "competitive_team": {"name": "Competitive Codegen Team",
        "panelists": ["specialist_codegen", "specialist_cpp", "specialist_go",
                      "reviewer_correctness", "reviewer_performance"],
        "synth": "specialist_chief"},
    # Flagship coding council: fast triage → parallel {codegen, architecture,
    # security, tests} → 30B chief that consumes all four and emits final code.
    "vera_prime": {"name": "Vera Prime",
        "triage": "triage",
        "panelists": ["specialist_codegen", "specialist_architecture",
                      "reviewer_security", "reviewer_testing"],
        "synth": "specialist_chief", "synth_model": MODEL_MAP["flagship"]},
}
_COLORS = ["#5b9dff", "#ee5a6f", "#5bd1a0", "#ffa94d", "#9b7cff", "#46c7c7"]


def _personas(vera_dir):
    """id → raw persona config dict from the vera_agents YAMLs."""
    out = {}
    for f in glob.glob(os.path.join(vera_dir, "*.yaml")):
        try:
            c = yaml.safe_load(open(f, encoding="utf-8").read())
            if isinstance(c, dict) and c.get("id"):
                out[c["id"]] = c
        except Exception:
            pass
    return out


def _persona_line(c: dict) -> str:
    toks = ", ".join((c.get("activation_tokens") or [])[:8])
    return (f"You are {c.get('name', c['id'])}, a {c.get('squad', '')} "
            f"{c.get('role', 'specialist')}. Focus: {toks}.")


def _endpoint_id(base: str) -> str:
    """Local Ollama endpoint id: env override, else the app's default endpoint."""
    ep = os.getenv("VERA_ENDPOINT_ID", "").strip()
    if ep:
        return ep
    try:
        with open(os.path.join(base, "data", "settings.json"), encoding="utf-8") as f:
            return (json.load(f).get("default_endpoint_id") or "").strip()
    except Exception:
        return ""


def build(vera_dir, out_dir, endpoint_id: str = ""):
    personas = _personas(vera_dir)
    os.makedirs(out_dir, exist_ok=True)
    made = []

    def _seat(pid, role, consumes, color, temperature, model=""):
        c = personas.get(pid)
        return {"id": pid, "name": pid, "role": role,
                "endpoint_id": endpoint_id,
                "model": model or (_pick_model(c) if c else MODEL_MAP["coder"]),
                "persona": _persona_line(c) if c else f"You are {pid}.",
                "color": color, "temperature": temperature,
                "consumes": consumes, "enabled": True}

    for tid, spec in TEAMS.items():
        present = [m for m in spec["panelists"] if m in personas] or spec["panelists"]
        seats = []
        first_layer = []
        triage_id = spec.get("triage")
        if triage_id:
            seats.append(_seat(triage_id, "panelist", [], "#46c7c7", 0.1))
            first_layer = [triage_id]
        for i, m in enumerate(present):
            seats.append(_seat(m, "panelist", list(first_layer),
                               _COLORS[i % len(_COLORS)], 0.2))
        seats.append(_seat(spec["synth"], "synthesizer", list(present), "#9b7cff",
                           0.3, model=spec.get("synth_model", "")))
        cfg = {"id": tid, "name": spec["name"], "seats": seats}
        # structural validation: exactly one synthesizer, no cycle, consumes resolve
        ids = {s["id"] for s in seats}
        assert len(ids) == len(seats), f"duplicate seat id in {tid}"
        assert sum(s["role"] == "synthesizer" for s in seats) == 1
        for s in seats:
            assert all(c in ids for c in s["consumes"]), f"dangling consume in {tid}"
        path = os.path.join(out_dir, tid + ".json")
        tmp = path + ".tmp"; open(tmp, "w", encoding="utf-8", newline="").write(json.dumps(cfg, indent=2)); os.replace(tmp, path)
        made.append((tid, len(seats)))
    return made


def main() -> int:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    vera_dir = os.path.join(base, "brains", "precision-audit", "config", "vera_agents")
    out_dir = os.path.join(base, "brains", "precision-audit", "config", "teams")
    endpoint_id = _endpoint_id(base)
    if not endpoint_id:
        print("  warn: no local endpoint id found (set VERA_ENDPOINT_ID or run "
              "scripts/wire_ollama_brain.py); seats will need one before running")
    made = build(vera_dir, out_dir, endpoint_id)
    for tid, n in made:
        print(f"  team '{tid}': {n} seats -> config/teams/{tid}.json")
    print(f"wrote {len(made)} team councils to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
