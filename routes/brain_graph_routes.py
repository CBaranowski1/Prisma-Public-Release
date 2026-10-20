"""Brain node-graph API — ports the llm-council `/api/brain` graph so the exact
same radial visualization renders inside Prisma.

Returns {nodes, links, events, stats} built from the Precision Audit vault
(knowledge/<category>/*.md) plus the council's active DAG (agents) and learned
events (conversations / lessons / reviews). Read-only, stdlib + optional PyYAML.
"""
from __future__ import annotations

import json
import re
import time as _time
from collections import Counter, defaultdict
from pathlib import Path

from fastapi import APIRouter, Request

from src.auth_helpers import require_privilege

router = APIRouter(tags=["brain-graph"])

# knowledge folder -> node type (mirrors the council enumeration, incl. the
# specialist/overseer clusters added later: web/bash/abap/oracle/overseer_*).
_KNOWLEDGE = [
    ("knowledge/code", "pack"), ("knowledge/concepts", "concept"),
    ("knowledge/ftz", "ftz"), ("knowledge/books", "book"),
    ("knowledge/trade", "trade"), ("knowledge/recipes", "recipe"),
    ("knowledge/security", "security"), ("knowledge/ruby", "ruby"),
    ("knowledge/maintainability", "maintainability"), ("knowledge/clarity", "clarity"),
    ("knowledge/prompting", "prompting"), ("knowledge/triage", "triage"),
    ("knowledge/correctness", "correctness"), ("knowledge/performance", "performance"),
    ("knowledge/redundancy", "redundancy"), ("knowledge/synthesis", "synthesis"),
    ("knowledge/overseer_eng", "overseer_eng"), ("knowledge/overseer_risk", "overseer_risk"),
    ("knowledge/web", "web"), ("knowledge/bash", "bash"),
    ("knowledge/abap", "abap"), ("knowledge/oracle", "oracle"),
    ("knowledge/postgres", "postgres"), ("knowledge/python", "python"),
    ("knowledge/codegen", "codegen"), ("knowledge/access", "access"),
    ("knowledge/domain", "domain"), ("knowledge/imported", "imported"),
    ("knowledge/agents", "agents"),
    ("knowledge/database", "database"), ("knowledge/erp", "erp"),
    ("knowledge/wms", "wms"),
    # the language + dimension specialist clusters added later
    ("knowledge/java", "java"), ("knowledge/csharp", "csharp"),
    ("knowledge/go", "go"), ("knowledge/rust", "rust"),
    ("knowledge/cpp", "cpp"), ("knowledge/php", "php"),
    ("knowledge/testing", "testing"), ("knowledge/api", "api"),
    ("knowledge/observability", "observability"),
    ("knowledge/documentation", "documentation"),
    ("knowledge/agentsdk", "agentsdk"),
    # tools cluster + new specialist + design-team clusters
    ("knowledge/tools", "tools"), ("knowledge/frontend", "frontend"),
    ("knowledge/devops", "devops"), ("knowledge/mobile", "mobile"),
    ("knowledge/dataeng", "dataeng"), ("knowledge/product", "product"),
    ("knowledge/ux", "ux"), ("knowledge/architecture", "architecture"),
    ("knowledge/backend", "backend"), ("knowledge/ml", "ml"),
    # self-learning reflections — one per conversation, linked to every agent
    ("knowledge/reflections", "reflection"),
    # new agent clusters from capability comparison (architecture/agentsdk already above)
    ("knowledge/accessibility", "accessibility"), ("knowledge/depsec", "depsec"),
    ("knowledge/concurrency", "concurrency"), ("knowledge/meta", "meta"),
    # round 2 lenses (backend/ml/ux/product already listed above)
    ("knowledge/privacy", "privacy"), ("knowledge/resilience", "resilience"),
    ("knowledge/migration", "migration"), ("knowledge/iac", "iac"),
    ("knowledge/finops", "finops"), ("knowledge/i18n", "i18n"),
    ("knowledge/licensing", "licensing"), ("knowledge/distributed", "distributed"),
    # gstack roles
    ("knowledge/ceo", "ceo"), ("knowledge/release", "release"),
    ("knowledge/devex", "devex"), ("knowledge/retro", "retro"),
]


def _knowledge_folders(vault: Path):
    """Return configured and discovered first-level knowledge folders.

    The historical council used a fixed category list, but recovered vaults
    contain additional clusters (for example ``cc_solutions``, ``exercises``,
    and ``leetcode``).  Keep the stable types for known clusters, then include
    every other real directory using a path-safe, deterministic type slug.
    """
    configured = {Path(sub).name for sub, _ in _KNOWLEDGE}
    for sub, ntype in _KNOWLEDGE:
        yield vault / sub, ntype

    root = vault / "knowledge"
    if not root.is_dir():
        return
    try:
        discovered = sorted(
            (
                child for child in root.iterdir()
                if child.is_dir() and not child.is_symlink()
                and child.name not in configured
            ),
            key=lambda child: child.name.casefold(),
        )
    except OSError:
        return
    for child in discovered:
        ntype = re.sub(r"[^a-z0-9_-]+", "-", child.name.casefold()).strip("-")
        yield child, ntype or "knowledge"

# agent seat id -> its knowledge cluster, so each agent links to its memory nodes
# (previously agents linked only to other agents and appeared to have 0 knowledge).
_SEAT_CLUSTER = {
    # specialist_mesh DAG seats (the precision-audit brain) -> their topic so
    # each agent links to its notes and lands on that topic's galaxy arm.
    "specialist_ruby_rails": "ruby", "specialist_msaccess_vba": "access",
    "specialist_typescript": "code", "ftz_compliance": "ftz", "ftz_systems": "ftz",
    "specialist_chief": "synthesis", "specialist_fallback": "imported",
    "triage": "triage", "reviewer_correctness": "correctness",
    "reviewer_performance": "performance", "reviewer_redundancy": "redundancy",
    "reviewer_security": "security", "reviewer_maintainability": "maintainability",
    "reviewer_clarity": "clarity", "reviewer_prompting": "prompting",
    "reviewer_testing": "testing", "reviewer_api": "api",
    "reviewer_observability": "observability", "reviewer_documentation": "documentation",
    "specialist_postgresql": "postgres", "specialist_oracle_plsql": "oracle",
    "specialist_ruby": "ruby", "specialist_python": "python", "specialist_web": "web",
    "specialist_bash": "bash", "specialist_abap": "abap", "specialist_java": "java",
    "specialist_csharp": "csharp", "specialist_go": "go", "specialist_rust": "rust",
    "specialist_cpp": "cpp", "specialist_php": "php", "specialist_agentsdk": "agentsdk",
    "specialist_database": "database", "specialist_erp": "erp", "specialist_ftz": "ftz",
    "specialist_wms": "wms", "overseer_mid_a": "overseer_eng",
    "overseer_mid_b": "overseer_risk", "overseer_chief": "synthesis",
    "specialist_frontend": "frontend", "specialist_devops": "devops",
    "specialist_mobile": "mobile", "specialist_dataeng": "dataeng",
    "product_manager": "product", "ux_designer": "ux",
    "solution_architect": "architecture", "frontend_engineer": "frontend",
    "backend_engineer": "backend", "mobile_engineer": "mobile",
    "data_engineer": "dataeng", "devops_engineer": "devops",
    "security_engineer": "security", "qa_engineer": "testing",
    "tech_writer": "documentation", "delivery_lead": "overseer_eng",
    "quality_lead": "overseer_risk", "eng_director": "synthesis",
    # security team
    "sec_appsec": "security", "sec_infra": "devops", "sec_detection": "observability",
    "sec_data": "database", "sec_api": "api", "sec_prompt": "prompting",
    "sec_lead_blue": "overseer_eng", "sec_lead_red": "overseer_risk", "sec_ciso": "synthesis",
    # maintainability team
    "maint_refactor": "maintainability", "maint_clarity": "clarity", "maint_arch": "architecture",
    "maint_test": "testing", "maint_docs": "documentation", "maint_debt": "redundancy",
    "maint_perf": "performance", "maint_lead": "overseer_eng", "maint_risk": "overseer_risk",
    "maint_chief": "synthesis",
    # data team
    "data_pipeline": "dataeng", "data_db": "database", "data_sql": "postgres",
    "data_oracle": "oracle", "data_quality": "testing", "data_gov": "security",
    "data_perf": "performance", "data_lead": "overseer_eng", "data_risk": "overseer_risk",
    "data_chief": "synthesis",
    # SRE team
    "sre_reliability": "observability", "sre_platform": "devops", "sre_perf": "performance",
    "sre_chaos": "testing", "sre_secops": "security", "sre_backend": "backend",
    "sre_lead": "overseer_eng", "sre_risk": "overseer_risk", "sre_chief": "synthesis",
    # supply-chain / trade team
    "sc_wms": "wms", "sc_erp": "erp", "sc_ftz": "ftz", "sc_trade": "trade",
    "sc_integration": "database", "sc_domain": "domain",
    "sc_lead": "overseer_eng", "sc_risk": "overseer_risk", "sc_chief": "synthesis",
    # product & UX team
    "pt_pm": "product", "pt_ux": "ux", "pt_design": "frontend",
    "pt_arch": "architecture", "pt_analytics": "dataeng",
    "pt_lead": "overseer_eng", "pt_risk": "overseer_risk", "pt_chief": "synthesis",
    # platform / cloud team
    "pf_platform": "devops", "pf_backend": "backend", "pf_arch": "architecture",
    "pf_observability": "observability", "pf_security": "security",
    "pf_lead": "overseer_eng", "pf_risk": "overseer_risk", "pf_chief": "synthesis",
    # ML / AI engineering team
    "ml_eng": "ml", "ml_data": "dataeng", "ml_eval": "testing",
    "ml_ops": "observability", "ml_safety": "security",
    "ml_lead": "overseer_eng", "ml_risk": "overseer_risk", "ml_chief": "synthesis",
    # full-stack application build team
    "fs_pm": "product", "fs_ux": "ux", "fs_frontend": "frontend", "fs_mobile": "mobile",
    "fs_api": "api", "fs_backend": "backend", "fs_db": "database", "fs_devops": "devops",
    "fs_qa": "testing", "fs_security": "security", "fs_arch": "architecture",
    "fs_docs": "documentation",
    "fs_lead": "overseer_eng", "fs_risk": "overseer_risk", "fs_chief": "synthesis",
    # agents added from capability comparison
    "specialist_architecture": "architecture", "specialist_agentsdk": "agentsdk",
    "reviewer_concurrency": "concurrency", "specialist_meta": "meta",
    "reviewer_accessibility": "accessibility", "reviewer_depsec": "depsec",
    # round 2
    "specialist_backend": "backend", "specialist_ml": "ml", "specialist_iac": "iac",
    "specialist_distributed": "distributed", "reviewer_resilience": "resilience",
    "reviewer_migration": "migration", "reviewer_privacy": "privacy",
    "reviewer_finops": "finops", "reviewer_i18n": "i18n", "reviewer_licensing": "licensing",
    "reviewer_ux": "ux", "reviewer_product": "product",
    # round 3
    "specialist_codegen": "codegen", "specialist_domain": "domain",
    "specialist_access": "access", "specialist_trade": "trade",
    # gstack roles + new/gap-fill team seats
    "specialist_ceo": "ceo", "specialist_release": "release",
    "specialist_devex": "devex", "specialist_retro": "retro",
    "ld_ceo": "ceo", "ld_pm": "product", "ld_arch": "architecture", "ld_design": "ux",
    "ld_devex": "devex", "ld_lead": "overseer_eng", "ld_risk": "overseer_risk", "ld_chief": "synthesis",
    "dl_release": "release", "dl_devops": "devops", "dl_sre": "observability", "dl_qa": "testing",
    "dl_resilience": "resilience", "dl_security": "security",
    "dl_lead": "overseer_eng", "dl_risk": "overseer_risk", "dl_chief": "synthesis",
    "sp_ceo": "ceo", "sp_arch": "architecture", "sp_design": "ux", "sp_build": "backend",
    "sp_review": "correctness", "sp_qa": "testing", "sp_security": "security",
    "sp_release": "release", "sp_retro": "retro",
    "sp_lead": "overseer_eng", "sp_risk": "overseer_risk", "sp_chief": "synthesis",
    "fs_a11y": "accessibility", "fs_observability": "observability", "fs_release": "release",
    "ds_a11y": "accessibility", "ds_observability": "observability",
    "sec_depsec": "depsec", "sec_privacy": "privacy",
    "sre_resilience": "resilience", "sre_release": "release",
    "pf_resilience": "resilience", "pf_finops": "finops",
    "pt_ceo": "ceo", "pt_devex": "devex", "data_privacy": "privacy",
}


def _vault_path() -> str:
    """Legacy single-vault fallback: saved setting `brain_vault_path` -> env."""
    try:
        from src.settings import get_setting
        saved = get_setting("brain_vault_path", "") or ""
        if saved:
            return saved
    except Exception:
        pass
    import os
    return os.getenv("PRISMA_BRAIN_VAULT", "")


def _resolve(brain: str = ""):
    """Resolve a brain to (knowledge_path, brain_dir). knowledge_path is where
    knowledge/ + config/ live (possibly an external vault); brain_dir is the
    brain folder (where a baked graph.json may live). A specific id -> that brain;
    empty/'all' -> the first discovered brain. Falls back to the legacy vault.

    An explicit PRISMA_BRAIN_VAULT env var is authoritative when set: it pins
    the graph to that vault (empty value = no vault), bypassing discovery of
    the repo-shipped brains/ folders — mirrors brain_ingest_routes and keeps
    tests hermetic on machines with a live install."""
    import os
    env = os.getenv("PRISMA_BRAIN_VAULT")
    if env is not None and not (brain or "").strip():
        return (Path(env), Path(env)) if env else (None, None)
    try:
        from src.brains import discover_brains, resolve_brain
        bid = (brain or "").split("brain:")[-1]
        b = None
        if bid and bid != "all":
            b = resolve_brain(bid)
        elif not bid:
            b = resolve_brain("llm-council")
        if not b:
            brains = discover_brains()
            b = brains[0] if brains else None
        if b:
            return Path(b["path"]), Path(b.get("dir") or b["path"])
    except Exception:
        pass
    vp = _vault_path()
    return (Path(vp), Path(vp)) if vp else (None, None)


def _title(p: Path, text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return (m.group(1).strip() if m else p.stem)[:80]


# Links are decorative (the renderer draws only a ~9k sample) but a big vault
# yields hundreds of thousands of them — the bulk of the payload + the memory
# that crashes the tab. Keep all nodes; evenly sample links down to this cap.
_LINK_CAP = 40000
_VISUAL_NODE_CAP = 9000
_VISUAL_LINK_CAP = 12000
_VISUAL_PRIORITY_TYPES = frozenset(
    {"agent", "conversation", "lesson", "review", "reflection"}
)
_WIKILINK_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,199}$")


def _cap_links(links, cap=_LINK_CAP):
    n = len(links)
    if n <= cap:
        return links
    step = n / cap
    return [links[int(i * step)] for i in range(cap)]


def _even_sample(items: list, count: int) -> list:
    """Return a stable sample spanning the complete ordered input."""
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def _visual_graph(
    graph: dict,
    node_cap: int = _VISUAL_NODE_CAP,
    link_cap: int = _VISUAL_LINK_CAP,
) -> dict:
    """Build a lightweight display graph without changing the indexed vault."""
    source_nodes = list(graph.get("nodes") or [])
    source_links = list(graph.get("links") or [])
    source_events = list(graph.get("events") or [])
    node_cap = max(1, int(node_cap))
    link_cap = max(0, int(link_cap))

    priority = [
        node for node in source_nodes
        if node.get("type") in _VISUAL_PRIORITY_TYPES
    ]
    if len(priority) > node_cap:
        agents = [node for node in priority if node.get("type") == "agent"]
        room = max(0, node_cap - len(agents))
        priority = agents[:node_cap] + _even_sample(
            [node for node in priority if node.get("type") != "agent"],
            room,
        )

    selected = list(priority)
    priority_ids = {str(node.get("id")) for node in priority}
    buckets: dict[str, list[dict]] = defaultdict(list)
    for node in source_nodes:
        if str(node.get("id")) not in priority_ids:
            buckets[str(node.get("type") or "knowledge")].append(node)

    room = max(0, node_cap - len(selected))
    total_regular = sum(len(bucket) for bucket in buckets.values())
    quotas: dict[str, int] = {}
    if room and total_regular:
        for category, bucket in buckets.items():
            quotas[category] = min(
                len(bucket),
                max(1, int(room * len(bucket) / total_regular)),
            )

        while sum(quotas.values()) > room:
            candidates = [
                category for category, quota in quotas.items() if quota > 1
            ]
            if not candidates:
                break
            category = max(candidates, key=lambda key: (quotas[key], key))
            quotas[category] -= 1
        while sum(quotas.values()) < room:
            candidates = [
                category for category, bucket in buckets.items()
                if quotas.get(category, 0) < len(bucket)
            ]
            if not candidates:
                break
            category = max(
                candidates,
                key=lambda key: (
                    len(buckets[key]) - quotas.get(key, 0),
                    key,
                ),
            )
            quotas[category] = quotas.get(category, 0) + 1

        for category in sorted(buckets):
            ordered = sorted(
                buckets[category],
                key=lambda node: str(node.get("id") or "").casefold(),
            )
            selected.extend(_even_sample(ordered, quotas.get(category, 0)))

    selected_ids = {str(node.get("id")) for node in selected}
    filtered_links = [
        link for link in source_links
        if str(link.get("source")) in selected_ids
        and str(link.get("target")) in selected_ids
    ]
    agent_ids = {
        str(node.get("id")) for node in selected if node.get("type") == "agent"
    }
    agent_links = []
    knowledge_links = []
    for link in filtered_links:
        target = (
            agent_links
            if str(link.get("source")) in agent_ids
            or str(link.get("target")) in agent_ids
            else knowledge_links
        )
        target.append(link)
    if len(agent_links) >= link_cap:
        visual_links = _even_sample(agent_links, link_cap)
    else:
        visual_links = agent_links + _even_sample(
            knowledge_links, link_cap - len(agent_links)
        )

    selected_event_ids = {
        str(node.get("id"))
        for node in selected
        if node.get("type") in {"conversation", "lesson", "review"}
    }
    visual_events = [
        event for event in source_events
        if str(event.get("id")) in selected_event_ids
    ]
    stats = dict(graph.get("stats") or {})
    stats.setdefault("nodes", len(source_nodes))
    stats.setdefault("links", len(source_links))
    stats.update({
        "visual_nodes": len(selected),
        "visual_links": len(visual_links),
        "visual_sample": len(selected) < len(source_nodes)
        or len(visual_links) < len(source_links),
    })
    return {
        "nodes": selected,
        "links": visual_links,
        "events": visual_events,
        "stats": stats,
    }


def _extract_wikilinks(text: str) -> list[str]:
    """Extract human-authored wiki targets, excluding code/array notation."""
    prose = re.sub(r"```.*?```", "", text or "", flags=re.DOTALL)
    prose = re.sub(r"`[^`]*`", "", prose)
    return [
        target
        for target in (
            raw.strip().casefold()
            for raw in re.findall(
                r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]",
                prose,
            )
        )
        if _WIKILINK_TARGET.fullmatch(target)
    ]


def _agents_from_dag(council_root: Path):
    """Read the active DAG's seats (agents) so the mesh shows around the brain.
    Returns (nodes, links) or ([], []) if PyYAML/config unavailable."""
    try:
        import yaml
    except Exception:
        return [], []
    try:
        cyaml = council_root / "config" / "council.yaml"
        dag_name = "aspect_specialists"
        if cyaml.is_file():
            cfg = yaml.safe_load(cyaml.read_text("utf-8", "replace")) or {}
            dag_name = cfg.get("dag", dag_name)
        dag_file = council_root / "config" / "dag" / f"{dag_name}.yaml"
        if not dag_file.is_file():
            return [], []
        spec = yaml.safe_load(dag_file.read_text("utf-8", "replace")) or {}
        seats = spec.get("seats", []) or []
        nodes, links = [], []
        for s in seats:
            sid = s.get("id")
            if not sid:
                continue
            nodes.append({"id": f"agent:{sid}", "type": "agent", "title": sid,
                          "role": s.get("role", ""), "lens": s.get("lens", ""),
                          "activation": s.get("activation", ["always"])})
        for s in seats:
            sid = s.get("id")
            for c in (s.get("consumes", []) or []):
                links.append({"source": f"agent:{sid}", "target": f"agent:{c}"})
        return nodes, links
    except Exception:
        return [], []


@router.get("/api/brain-graph")
async def brain_graph(request: Request, brain: str = "", visual: bool = True) -> dict:
    # SECURITY (audit P3): vault graph metadata is brain-privileged. Gate reads
    # behind can_manage_brain so users without the Brain privilege can't
    # enumerate the knowledge vault. No-op for admins / single-user mode.
    require_privilege(request, "can_manage_brain", fail_closed=True)
    know, bdir = _resolve(brain)
    if not know:
        return {"nodes": [], "links": [], "events": [],
                "stats": {"counts": {}, "nodes": 0, "links": 0, "now": _time.time(),
                          "error": "no brain vault configured"}}
    # Serve a pre-baked galaxy if the brain has one (tools/bake_brain_graph.py) —
    # this is how a big vault (tens of thousands of notes) loads instantly instead
    # of being globbed live on every open.
    if bdir:
        baked = bdir / "graph.json"
        if baked.is_file():
            try:
                from fastapi import Response
                payload = baked.read_bytes()
                if not visual:
                    return Response(content=payload, media_type="application/json")
                # New baked files are already display-sized. Older Spark
                # releases can still have the former ~25 MB full graph, so
                # compact that payload during the transition.
                if b'"visual_nodes"' in payload:
                    return Response(content=payload, media_type="application/json")
                return _visual_graph(json.loads(payload))
            except Exception:
                pass
    graph = _build_graph(know)
    return _visual_graph(graph) if visual else graph


def _build_graph(vault: Path) -> dict:
    """Build the {nodes, links, events, stats} galaxy data from a vault folder
    (knowledge/<cat>/*.md + config/ agents + conversation/lesson/review events).
    Pure read; used live by the endpoint and offline by the bake tool."""
    nodes: list[dict] = []
    links: list[dict] = []
    events: list[dict] = []
    ids: set[str] = set()
    cluster_notes: dict[str, list[str]] = {}

    # Enumerate first so duplicate slugs across language/category variants can
    # receive distinct graph ids. The original slug and cluster travel with the
    # node so the note endpoint can still open the exact backing file.
    note_files: list[tuple[Path, str, str]] = []
    for d, ntype in _knowledge_folders(vault):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.stem.startswith("_"):
                continue
            note_files.append((p, ntype, d.name))

    slug_counts = Counter(p.stem.casefold() for p, _, _ in note_files)
    note_ids: dict[tuple[str, str], str] = {}
    canonical_ids: dict[str, str] = {}
    for p, ntype, cluster in note_files:
        slug_key = p.stem.casefold()
        nid = p.stem if slug_counts[slug_key] == 1 else f"{ntype}:{p.stem}"
        note_ids[(cluster.casefold(), slug_key)] = nid
        canonical_ids.setdefault(slug_key, nid)

    pending_links: list[tuple[str, str, str]] = []
    for p, ntype, cluster in note_files:
        slug_key = p.stem.casefold()
        nid = note_ids[(cluster.casefold(), slug_key)]
        try:
            text = p.read_text("utf-8", "replace")
        except OSError:
            continue
        nodes.append({
            "id": nid,
            "slug": p.stem,
            "cluster": cluster,
            "type": ntype,
            "title": _title(p, text),
        })
        ids.add(nid)
        cluster_notes.setdefault(ntype, []).append(nid)
        for target in _extract_wikilinks(text):
            pending_links.append((nid, cluster.casefold(), target))

    for source, cluster, target_slug in pending_links:
        target = (
            note_ids.get((cluster, target_slug))
            or canonical_ids.get(target_slug)
        )
        if target:
            links.append({"source": source, "target": target})

    # agent mesh from the council DAG. Modular brains keep their config INSIDE
    # the brain folder (brains/<id>/config); legacy external vaults keep it in
    # the parent — try the brain folder first, then fall back to the parent.
    a_nodes, a_links = _agents_from_dag(vault)
    if not a_nodes:
        a_nodes, a_links = _agents_from_dag(vault.parent)
    for n in a_nodes:
        nodes.append(n); ids.add(n["id"])
    links.extend(a_links)
    # link each agent to its OWN knowledge cluster notes so the galaxy shows its
    # memory (previously agents linked only to other agents and appeared to have 0).
    for n in a_nodes:
        cl = _SEAT_CLUSTER.get(n["id"].split(":", 1)[1])
        if cl:
            for slug in cluster_notes.get(cl, []):
                links.append({"source": n["id"], "target": slug})
    # self-learning: EVERY agent links to EVERY reflection note (shared memory)
    for slug in cluster_notes.get("reflection", []):
        for n in a_nodes:
            links.append({"source": n["id"], "target": slug})

    known = {n["id"] for n in nodes}
    name_index = {}
    for n in nodes:
        name_index.setdefault(n.get("slug", n["id"]).casefold(), n["id"])

    def _add_events(glob_pat: str, etype: str, base: Path):
        for p in sorted(base.glob(glob_pat)):
            try:
                text = p.read_text("utf-8", "replace")
            except OSError:
                continue
            nid = f"{etype}:{p.stem}"
            ts = p.stat().st_mtime
            nodes.append({"id": nid, "type": etype, "title": _title(p, text), "ts": ts})
            ids.add(nid)
            events.append({"id": nid, "type": etype, "ts": ts,
                           "title": _title(p, text),
                           "preview": re.sub(r"\s+", " ", text)[:160]})
            low = text.lower()
            linked = 0
            for nm, real in name_index.items():
                if linked >= 4:
                    break
                if re.search(r"\b" + re.escape(nm) + r"\b", low):
                    links.append({"source": nid, "target": real})
                    linked += 1

    _add_events("conversations/*.md", "conversation", vault)
    _add_events("lessons/*.md", "lesson", vault)
    _add_events("review-*.md", "review", vault)

    links = [ln for ln in links if ln["source"] in ids and ln["target"] in known]
    events.sort(key=lambda e: e["ts"])

    # Keep ALL nodes (the stars = the density), but cap links: the 3D renderer
    # only ever draws a ~9k sample of them, while a huge vault can produce
    # hundreds of thousands of link objects that blow up browser memory (the
    # "kills my computer" crash). Evenly sample down — visually identical.
    links = _cap_links(links, _LINK_CAP)

    counts: dict[str, int] = {}
    for n in nodes:
        counts[n["type"]] = counts.get(n["type"], 0) + 1
    return {"nodes": nodes, "links": links, "events": events,
            "stats": {"counts": counts, "nodes": len(nodes), "links": len(links),
                      "now": _time.time()}}


@router.get("/api/brain-note")
async def brain_note(
    request: Request, slug: str = "", brain: str = "", cluster: str = ""
) -> dict:
    """Return the markdown of a single vault knowledge note by its slug (file stem).
    Used by the 3D galaxy: click a node -> read its .md. Read-only, path-safe."""
    # SECURITY (audit P3): note markdown is brain-privileged content.
    require_privilege(request, "can_manage_brain", fail_closed=True)
    vault, _ = _resolve(brain)
    if not vault:
        return {"error": "no brain vault configured"}
    # strict slug allowlist — blocks path traversal / separators entirely
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", slug or ""):
        return {"error": "invalid slug"}
    if cluster and not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", cluster):
        return {"error": "invalid cluster"}
    base = (vault / "knowledge").resolve()
    candidates = []
    if cluster:
        candidates.append(base / cluster / f"{slug}.md")
    candidates.extend(
        path for path in sorted(base.glob("*/" + slug + ".md"))
        if path not in candidates
    )
    for p in candidates:
        if not p.is_file():
            continue
        rp = p.resolve()
        if base not in rp.parents:   # never escape the knowledge tree
            continue
        try:
            text = rp.read_text("utf-8", "replace")
        except OSError:
            break
        # outbound wikilinks (for navigation in the viewer)
        links = sorted(set(_extract_wikilinks(text)))
        return {"slug": slug, "cluster": rp.parent.name,
                "title": _title(rp, text), "text": text, "links": links}
    return {"error": "not found"}
