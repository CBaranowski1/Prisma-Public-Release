"""VERA agent registry + council translator API.

Loads the VERA config-as-data agents under each brain at
`brains/<brain>/config/vera_agents/*.yaml`, serves them read-only, and
translates a selection of them into a prism `CouncilConfig` that
`/api/council/run` already knows how to run.

Read-only and side-effect free: a malformed or missing config is skipped and
reported in `errors`, never raised to the caller.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
import uuid
from collections import deque
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException, Request

from core.constants import BASE_DIR

router = APIRouter(prefix="/api/brain", tags=["brain"])

_REQUIRED = ("id", "name", "squad", "activation_tokens", "model_policy", "tools")

_ROLE2SEAT = {
    "specialist": "panelist", "reviewer": "panelist", "triage": "panelist",
    "mid_overseer": "aggregator", "overseer": "aggregator", "chief": "synthesizer",
}
_SQUAD_COLOR = {"build": "#5b9dff", "quality": "#ee5a6f", "research": "#5bd1a0",
                "ops": "#ffa94d", "meta": "#9b7cff"}

_ACTIVITY_LOCK = threading.RLock()
_ACTIVITIES: deque[dict] = deque(maxlen=200)
_ACTIVITY_LOADED = False


def _activity_path() -> Path:
    return Path(BASE_DIR) / "brain_activity.json"


def _activity_owner(request: Request) -> str:
    try:
        from src.auth_helpers import get_current_user
        return str(get_current_user(request) or "")
    except Exception:
        return ""


def _ensure_activity_loaded() -> None:
    global _ACTIVITY_LOADED
    with _ACTIVITY_LOCK:
        if _ACTIVITY_LOADED:
            return
        _ACTIVITY_LOADED = True
        try:
            payload = json.loads(_activity_path().read_text("utf-8"))
            for item in (payload.get("activities") or [])[-200:]:
                if isinstance(item, dict) and item.get("id"):
                    _ACTIVITIES.append(item)
        except (OSError, ValueError, TypeError):
            pass


def _persist_activities() -> None:
    path = _activity_path()
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({"activities": list(_ACTIVITIES)}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _clean_retrieval(item: dict) -> dict:
    return {
        "provider": str(item.get("provider") or "")[:100],
        "provider_name": str(item.get("provider_name") or "")[:120],
        "id": str(item.get("id") or "")[:220],
        "category": str(item.get("category") or "")[:100],
        "source": str(item.get("source") or "")[:220],
        "score": round(float(item.get("score") or 0), 5),
        "characters": int(item.get("characters") or 0),
    }


def record_brain_activity(
    *,
    owner: str = "",
    session_id: str = "",
    query: str = "",
    profile: dict | None = None,
    profile_auto: bool = False,
    brain_used: list | None = None,
    memories_used: int = 0,
    rag_sources: int = 0,
    agent_mode: bool = False,
    brain_latency_ms: float = 0,
    context_latency_ms: float = 0,
    reasoning_requested: str = "",
    reasoning_effective: str = "",
    reasoning_route: str = "",
) -> str:
    """Persist the real context-assembly trace used by the Neural Brain UI."""
    _ensure_activity_loaded()
    now = time.time()
    activity_id = uuid.uuid4().hex[:16]
    safe_profile = None
    if profile:
        safe_profile = {
            "id": str(profile.get("id") or "")[:120],
            "name": str(profile.get("name") or profile.get("id") or "")[:160],
            "brain": str(profile.get("brain") or "")[:100],
            "role": str(profile.get("role") or "")[:80],
            "squad": str(profile.get("squad") or "")[:80],
            "activation_tokens": [str(v)[:80] for v in (profile.get("activation_tokens") or [])[:12]],
        }
    record = {
        "id": activity_id,
        "owner": str(owner or "")[:180],
        "session_id": str(session_id or "")[:180],
        "query": " ".join(str(query or "").split())[:240],
        "profile": safe_profile,
        "profile_auto": bool(profile_auto),
        "retrievals": [_clean_retrieval(v) for v in (brain_used or []) if isinstance(v, dict)][:20],
        "memories_used": max(0, int(memories_used or 0)),
        "rag_sources": max(0, int(rag_sources or 0)),
        "agent_mode": bool(agent_mode),
        "brain_latency_ms": round(max(0.0, float(brain_latency_ms or 0)), 1),
        "context_latency_ms": round(max(0.0, float(context_latency_ms or 0)), 1),
        "reasoning_requested": str(reasoning_requested or "")[:20],
        "reasoning_effective": str(reasoning_effective or "")[:20],
        "reasoning_route": str(reasoning_route or "")[:160],
        "state": "context_ready",
        "started_at": now,
        "updated_at": now,
        "metrics": {},
        "agent_rounds": 0,
        "tool_calls": 0,
        "feedback": "",
    }
    with _ACTIVITY_LOCK:
        _ACTIVITIES.append(record)
        _persist_activities()
    return activity_id


def complete_brain_activity(
    activity_id: str,
    *,
    metrics: dict | None = None,
    agent_rounds: int = 0,
    tool_calls: int = 0,
    state: str = "complete",
) -> None:
    if not activity_id:
        return
    _ensure_activity_loaded()
    allowed_metrics = {
        "response_time", "input_tokens", "output_tokens", "tokens_per_second",
        "context_percent", "context_length", "model", "usage_source",
        "reasoning_effort", "reasoning_requested", "reasoning_route",
        "grounding_status", "evidence_count", "cited_source_count",
        "invalid_citation_count",
    }
    with _ACTIVITY_LOCK:
        for record in reversed(_ACTIVITIES):
            if record.get("id") != activity_id:
                continue
            record["state"] = state if state in {"complete", "failed", "stopped"} else "complete"
            record["updated_at"] = time.time()
            record["agent_rounds"] = max(0, int(agent_rounds or 0))
            record["tool_calls"] = max(0, int(tool_calls or 0))
            clean_metrics = {}
            for key, value in (metrics or {}).items():
                if key in allowed_metrics and isinstance(value, (str, int, float, bool)):
                    clean_metrics[key] = value
            record["metrics"] = clean_metrics
            _persist_activities()
            return


def _activity_stats(records: list[dict]) -> dict:
    completed = [r for r in records if r.get("state") == "complete"]
    rated = [r for r in records if r.get("feedback") in {"helpful", "missed"}]
    helpful = sum(1 for r in rated if r.get("feedback") == "helpful")
    retrieval_runs = [
        r for r in completed
        if r.get("retrievals") or r.get("memories_used") or r.get("rag_sources")
    ]
    brain_times = [float(r.get("brain_latency_ms") or 0) for r in completed if r.get("brain_latency_ms") is not None]
    response_times = [float((r.get("metrics") or {}).get("response_time") or 0) for r in completed if (r.get("metrics") or {}).get("response_time")]
    return {
        "runs": len(records),
        "completed": len(completed),
        "retrieval_rate": round((len(retrieval_runs) / len(completed) * 100), 1) if completed else 0,
        "helpful_rate": round((helpful / len(rated) * 100), 1) if rated else None,
        "rated": len(rated),
        "avg_brain_ms": round(sum(brain_times) / len(brain_times), 1) if brain_times else 0,
        "avg_response_s": round(sum(response_times) / len(response_times), 2) if response_times else 0,
    }


def _agents_root() -> str:
    return os.path.join(BASE_DIR, "brains")


def _load_all() -> dict:
    agents = []
    errors = []
    pattern = os.path.join(_agents_root(), "*", "config", "vera_agents", "*.yaml")
    for path in sorted(glob.glob(pattern)):
        brain = path.split(os.sep + "brains" + os.sep, 1)[-1].split(os.sep, 1)[0]
        try:
            with open(path, encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            missing = [k for k in _REQUIRED if k not in cfg]
            if missing:
                errors.append({"file": os.path.basename(path), "brain": brain,
                               "error": "missing keys: " + str(missing)})
                continue
            cfg["brain"] = brain
            agents.append(cfg)
        except Exception as e:
            errors.append({"file": os.path.basename(path), "brain": brain,
                           "error": str(e)[:160]})
    by_squad = {}
    by_brain = {}
    for a in agents:
        by_squad.setdefault(a.get("squad", "?"), []).append(a["id"])
        by_brain.setdefault(a["brain"], []).append(a["id"])
    return {"agents": agents, "by_squad": by_squad, "by_brain": by_brain, "errors": errors}


@lru_cache(maxsize=1)
def _cached() -> dict:
    return _load_all()


def catalog_summary() -> dict:
    """Return stable VERA inventory counts for capability manifests."""
    agents = _cached()["agents"]
    unique_ids = {str(agent.get("id") or "") for agent in agents if agent.get("id")}
    return {
        "unique_profiles": len(unique_ids),
        "source_configs": len(agents),
        "brains": sorted({str(agent.get("brain") or "") for agent in agents if agent.get("brain")}),
    }


def catalog_documents(query: str = "", limit: int = 20) -> list[dict]:
    """Expose validated VERA profiles as virtual Brain documents.

    The historical YAML files live under other brains' ``config/`` trees, so
    the dedicated ``vera-brain`` vault previously searched only its welcome
    note and looked empty. These rows make the catalog searchable without
    copying or rewriting the YAML sources.
    """
    import re

    raw_terms = set(re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", str(query or "").lower()))
    # Catalog nouns describe the collection rather than one profile. A search
    # for "agent" or "VERA profiles" should browse the roster, not happen to
    # match only specialist_agentsdk.
    q_terms = raw_terms - {"agent", "agents", "profile", "profiles", "vera", "skill", "skills"}
    rows = []
    for agent in _cached()["agents"]:
        tokens = [str(v) for v in (agent.get("activation_tokens") or [])]
        haystack = " ".join([
            str(agent.get("id") or ""), str(agent.get("name") or ""),
            str(agent.get("role") or ""), str(agent.get("squad") or ""),
            str(agent.get("brain") or ""), " ".join(tokens),
        ]).lower()
        matched = sum(1 for term in q_terms if term in haystack)
        if q_terms and not matched:
            continue
        agent_id = str(agent.get("id") or "")
        source_brain = str(agent.get("brain") or "")
        # Brain documents use normalized cosine/hybrid scores.  A raw word
        # count here put a one-token profile match at 1.0 and let it outrank an
        # exact imported document with a stale vector.  Query-term coverage is
        # comparable and keeps partial profile matches partial.
        score = matched / len(q_terms) if q_terms else 0.0
        rows.append({
            "id": f"vera-agent:{source_brain}:{agent_id}",
            "title": str(agent.get("name") or agent_id),
            "snippet": "%s · %s · source %s · activates on %s" % (
                str(agent.get("role") or "profile"),
                str(agent.get("squad") or "general"),
                source_brain or "unknown",
                ", ".join(tokens[:8]) or "explicit selection",
            ),
            "category": "vera-agents",
            "source_path": f"brains/{source_brain}/config/vera_agents/{agent_id}.yaml",
            "score": float(score),
            "brain_id": "brain:vera-brain",
            "brain_name": "VERA Brain",
            "kind": "vera_profile",
            "agent_id": agent_id,
            "role": str(agent.get("role") or ""),
            "squad": str(agent.get("squad") or ""),
            "source_brain": source_brain,
        })
    rows.sort(key=lambda row: (-row["score"], row["title"].lower(), row["source_brain"]))
    return rows if not raw_terms or not limit else rows[:limit]


def _persona(agent: dict) -> str:
    brain = agent.get("brain", "")
    src = agent.get("source_prompt") or (agent["id"] + ".md")
    p = os.path.join(_agents_root(), brain, "config", "prompts", src)
    try:
        with open(p, encoding="utf-8") as fh:
            txt = fh.read().strip()
        if txt:
            return txt[:1500]
    except OSError:
        pass
    toks = ", ".join(agent.get("activation_tokens", [])[:8])
    return ("You are " + str(agent.get("name", agent["id"])) + ", a "
            + str(agent.get("squad", "")) + " " + str(agent.get("role", "specialist"))
            + ". Focus areas: " + toks + ".")


def get_agent_profile(agent_id: str, brain: str = "") -> dict | None:
    """Resolve one VERA profile, optionally selecting its brain variant.

    Several historical profiles intentionally exist in both brains.  A caller
    that names a brain gets that exact variant; otherwise the first stable
    catalog entry is returned for backwards compatibility.
    """
    wanted_id = str(agent_id or "").strip()
    wanted_brain = str(brain or "").removeprefix("brain:").strip()
    if not wanted_id:
        return None
    matches = [a for a in _cached()["agents"] if a.get("id") == wanted_id]
    if wanted_brain:
        matches = [a for a in matches if a.get("brain") == wanted_brain]
    return matches[0] if matches else None


def match_agent_profile(query: str, brain: str = "") -> dict | None:
    """Conservatively route a clear Agent-mode intent to one VERA profile.

    Generic tokens such as ``code`` and ``review`` never trigger alone.  This
    keeps ordinary conversation neutral while making explicit domains such as
    PostgreSQL, accessibility, FTZ, Rust, or security use their recovered
    specialist automatically.
    """
    import re

    words = set(re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", str(query or "").lower()))
    if not words:
        return None
    generic = {
        "code", "review", "skill", "skills", "agent", "agents", "brain",
        "build", "implement", "create", "make", "fix", "profile", "profiles",
        "runtime", "tool", "tools", "count", "counts", "inventory",
    }
    wanted_brain = str(brain or "").removeprefix("brain:").strip()
    candidates = _cached()["agents"]
    if wanted_brain:
        scoped = [a for a in candidates if a.get("brain") == wanted_brain]
        if scoped:
            candidates = scoped

    ranked = []
    for agent in candidates:
        tokens = {str(v).lower() for v in (agent.get("activation_tokens") or [])}
        specific = (tokens - generic) & words
        if not specific:
            continue
        score = len(specific) * 10
        role = str(agent.get("role") or "")
        agent_id = str(agent.get("id") or "")
        domain = re.sub(r"^(specialist|reviewer|general)_", "", agent_id)
        if domain in words:
            score += 12
        if "review" in words and role == "reviewer":
            score += 7
        if words & {"build", "implement", "create", "fix", "code"} and role == "specialist":
            score += 5
        # Prefer the richer Precision variant when duplicate historical IDs tie.
        if agent.get("brain") == "precision-audit":
            score += 1
        ranked.append((score, len(tokens), agent_id, agent))
    if not ranked:
        return None
    ranked.sort(key=lambda value: (value[0], value[1], value[2]), reverse=True)
    return ranked[0][3]


def runtime_prompt(agent_id: str, brain: str = "") -> tuple[dict, str]:
    """Return the validated profile and the bounded prompt used by chat.

    VERA tool declarations are workflow preferences from the recovered brain,
    not new permissions.  The runtime prompt says that explicitly so selecting
    a profile can specialize the model without bypassing Prisma's tool gates.
    """
    agent = get_agent_profile(agent_id, brain)
    if agent is None:
        raise KeyError(agent_id)

    protocol = agent.get("local_protocol") or {}
    enabled_protocol = [str(k) for k, v in protocol.items() if v is True]
    output_contract = [str(v) for v in (agent.get("output_contract") or [])[:12]]
    approvals = [str(v) for v in (agent.get("approval_required") or [])[:12]]
    security = [str(v) for v in (agent.get("security_required") or [])[:12]]

    lines = [
        "[Active VERA runtime profile]",
        f"Profile: {agent.get('name', agent['id'])} ({agent['id']})",
        f"Role: {agent.get('role', 'specialist')} · Squad: {agent.get('squad', 'general')} · Brain: {agent.get('brain', '')}",
        _persona(agent),
        "Apply this profile as task-specialization guidance for the current response.",
        "The profile's tool lists do not grant permissions. Use only tools exposed by Prisma, and keep all normal approval, safety, and access controls.",
    ]
    if enabled_protocol:
        lines.append("Preferred workflow: " + ", ".join(enabled_protocol) + ".")
    if output_contract:
        lines.append("Preferred answer contract, when relevant: " + ", ".join(output_contract) + ".")
    if approvals:
        lines.append("Explicit approval remains required for: " + ", ".join(approvals) + ".")
    if security:
        lines.append("Verification expectations, when relevant: " + ", ".join(security) + ".")
    return agent, "\n".join(lines)[:6000]


def build_council(agents: list, council_id: str, name: str, limit: int) -> dict:
    from src.council.schemas import CouncilConfig

    panel, aggs, synth = [], [], []
    for a in agents:
        sr = _ROLE2SEAT.get(a.get("role", "specialist"), "panelist")
        (synth if sr == "synthesizer" else aggs if sr == "aggregator" else panel).append(a)

    panel = panel[:max(1, limit)]
    aggs = aggs[:3]
    chief = synth[0] if synth else None
    for extra in synth[1:]:
        aggs.append(extra)

    def seat(a, role, consumes):
        return {"id": a["id"], "name": a.get("name", a["id"]), "role": role,
                "endpoint_id": "", "model": "",
                "persona": _persona(a),
                "color": _SQUAD_COLOR.get(a.get("squad", ""), "#5b9dff"),
                "temperature": 0.0 if a.get("role") == "triage" else 0.3,
                "consumes": consumes, "enabled": True}

    seats = []
    panel_ids = [a["id"] for a in panel]
    for a in panel:
        seats.append(seat(a, "panelist", []))
    agg_ids = []
    for a in aggs:
        seats.append(seat(a, "aggregator", panel_ids))
        agg_ids.append(a["id"])
    if chief is None:
        chief = {"id": council_id + "_chief", "name": "Synthesizer", "role": "chief",
                 "squad": "meta", "activation_tokens": [],
                 "brain": agents[0].get("brain", "") if agents else ""}
    seats.append(seat(chief, "synthesizer", agg_ids or panel_ids))

    cfg = {"id": council_id, "name": name, "seats": seats}
    CouncilConfig(**cfg).layers()  # validate + raise on cycle / dangling dep
    return cfg


@router.get("/agents")
async def list_agents(brain: str = "", squad: str = "", q: str = "") -> dict:
    data = _cached()
    agents = data["agents"]
    if brain:
        agents = [a for a in agents if a.get("brain") == brain]
    if squad:
        agents = [a for a in agents if a.get("squad") == squad]
    if q:
        terms = [part for part in str(q).lower().split() if part]
        agents = [
            a for a in agents
            if all(term in " ".join([
                str(a.get("id") or ""), str(a.get("name") or ""),
                str(a.get("role") or ""), str(a.get("squad") or ""),
                str(a.get("brain") or ""),
                " ".join(str(v) for v in (a.get("activation_tokens") or [])),
            ]).lower() for term in terms)
        ]
    summary = [{"id": a["id"], "name": a["name"], "squad": a.get("squad"),
                "role": a.get("role", ""), "brain": a.get("brain"),
                "model": (a.get("model_policy") or {}).get("default"),
                "activation_tokens": a.get("activation_tokens", [])}
               for a in agents]
    return {"count": len(summary), "agents": summary,
            "by_squad": data["by_squad"], "by_brain": data["by_brain"],
            "runtime": {
                "searchable_in_brain": True,
                "selectable_in_ui": True,
                "chat_prompt_injection": True,
                "automatic_domain_matching": True,
                "autonomous_background_workers": False,
            },
            "errors": data["errors"]}


@router.get("/agents/council")
async def agents_council(brain: str = "", squad: str = "", limit: int = 8) -> dict:
    agents = _cached()["agents"]
    if brain:
        agents = [a for a in agents if a.get("brain") == brain]
    if squad:
        agents = [a for a in agents if a.get("squad") == squad]
    if not agents:
        raise HTTPException(404, "no VERA agents match that filter")
    cid = ("vera_" + (squad or "all")).lower()
    nm = "VERA Council (" + (squad or "mixed") + ")"
    try:
        council = build_council(agents, cid, nm, max(1, min(limit, 16)))
    except Exception as e:
        raise HTTPException(500, "could not build council: " + str(e))
    return {"council": council, "seat_count": len(council["seats"]),
            "from_agents": len(agents)}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, brain: str = "") -> dict:
    agent = get_agent_profile(agent_id, brain)
    if agent is not None:
        return agent
    raise HTTPException(404, "unknown VERA agent: " + agent_id)


@router.get("/activity")
async def brain_activity(request: Request, limit: int = 12) -> dict:
    """Return recent context traces for the current user and aggregate quality signals."""
    _ensure_activity_loaded()
    owner = _activity_owner(request)
    with _ACTIVITY_LOCK:
        records = [
            dict(item) for item in _ACTIVITIES
            if not owner or not item.get("owner") or item.get("owner") == owner
        ]
    records = records[-max(1, min(int(limit or 12), 50)):]
    records.reverse()
    return {"activities": records, "stats": _activity_stats(records)}


@router.post("/activity/{activity_id}/feedback")
async def brain_activity_feedback(activity_id: str, request: Request, payload: dict) -> dict:
    """Attach an explicit retrieval-quality label used by the improvement loop."""
    value = str((payload or {}).get("feedback") or "").strip().lower()
    if value not in {"helpful", "missed", "clear"}:
        raise HTTPException(400, "feedback must be helpful, missed, or clear")
    owner = _activity_owner(request)
    _ensure_activity_loaded()
    with _ACTIVITY_LOCK:
        for record in reversed(_ACTIVITIES):
            if record.get("id") != activity_id:
                continue
            if owner and record.get("owner") and record.get("owner") != owner:
                raise HTTPException(404, "activity not found")
            record["feedback"] = "" if value == "clear" else value
            record["updated_at"] = time.time()
            _persist_activities()
            return {"ok": True, "id": activity_id, "feedback": record["feedback"]}
    raise HTTPException(404, "activity not found")
