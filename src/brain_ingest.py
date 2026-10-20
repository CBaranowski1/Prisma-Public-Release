"""Rule-based formatter for ingesting scraped .md into the brain vault.

`format_note` is the single seam a future local-model pass can replace: it
chooses a category, derives a title/slug, and injects YAML frontmatter without
touching the body. Category names are the single source of truth shared with
routes/brain_graph_routes.py.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# KEEP-IN-SYNC with routes/brain_graph_routes.py::_KNOWLEDGE. This is a
# vendored, static snapshot of the CURRENT live tag set (read directly from
# that module, not from any stale doc/example) used ONLY when the import
# below fails. Drift risk: if _KNOWLEDGE grows or shrinks, this list will
# NOT auto-update -- a degrade window would then miss new tags (or keep
# stale ones) until this constant is refreshed by hand. The fail-loud log
# below is the mitigation: any time this fallback is actually used, it is
# logged at ERROR so the drift is visible in production logs rather than
# silently narrowing categorization forever (GRAY-BRN-12 / T-30-tax-drift).
_FALLBACK_CATEGORIES = frozenset({
    "pack", "concept", "ftz", "book", "trade", "recipe", "security", "ruby",
    "maintainability", "clarity", "prompting", "triage", "correctness",
    "performance", "redundancy", "synthesis", "overseer_eng", "overseer_risk",
    "web", "bash", "abap", "oracle", "postgres", "python", "codegen", "access",
    "domain", "imported", "agents", "database", "erp", "wms", "java", "csharp",
    "go", "rust", "cpp", "php", "testing", "api", "observability",
    "documentation", "agentsdk", "tools", "frontend", "devops", "mobile",
    "dataeng", "product", "ux", "architecture", "backend", "ml", "reflection",
    "accessibility", "depsec", "concurrency", "meta", "privacy", "resilience",
    "migration", "iac", "finops", "i18n", "licensing", "distributed", "ceo",
    "release", "devex", "retro",
})

# Single source of truth for category names: the graph route's knowledge map.
try:
    from routes.brain_graph_routes import _KNOWLEDGE as _KN
    KNOWN_CATEGORIES = frozenset(t for _, t in _KN) | {"imported"}
except Exception as e:  # pragma: no cover - route import is normally available
    logger.error(
        "brain_ingest: failed to import routes.brain_graph_routes._KNOWLEDGE; "
        "falling back to vendored static category list (%d categories): %s",
        len(_FALLBACK_CATEGORIES), e, exc_info=True,
    )
    KNOWN_CATEGORIES = _FALLBACK_CATEGORIES

# Lightweight keyword hints for the deterministic categorizer. Only a few
# high-signal buckets; everything else falls back to "imported".
_KEYWORDS = {
    "security": ("security", "vulnerability", "cve", "exploit", "auth", "xss", "injection", "secret"),
    "performance": ("performance", "latency", "throughput", "optimi", "slow", "benchmark"),
    "testing": ("test", "pytest", "unittest", "coverage", "assert"),
    "python": ("python", "pip", "asyncio", "pytest", "django", "flask"),
    "oracle": ("oracle", "pl/sql", "plsql"),
    "postgres": ("postgres", "psql", "pgvector"),
    "abap": ("abap", "sap"),
    "bash": ("bash", "shell script", "powershell"),
    "web": ("javascript", "typescript", "react", "css", "html"),
    "documentation": ("readme", "docs", "documentation"),
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    return s or "note"


def _frontmatter(text: str) -> dict:
    m = _FM_RE.match(text or "")
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip().strip('"').strip("'")
    return out


def _strip_frontmatter(text: str) -> str:
    return _FM_RE.sub("", text or "", count=1)


def _title(body: str, filename: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if m:
        return m.group(1).strip()[:120]
    stem = re.sub(r"\.md$", "", (filename or "").strip(), flags=re.I)
    stem = re.sub(r"[-_]+", " ", stem).strip()
    return (stem or "Imported Note").title()[:120]


def _categorize(text: str) -> str:
    low = (text or "").lower()
    best, best_hits = "imported", 0
    for cat, words in _KEYWORDS.items():
        if cat not in KNOWN_CATEGORIES:
            continue
        # Word-boundary counts (not bare substring) so "auth" doesn't match
        # "author", "sap" doesn't match "sapient", "test" doesn't match "latest".
        hits = sum(len(re.findall(r"\b" + re.escape(w) + r"\b", low)) for w in words)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def choose_category(text: str, explicit: Optional[str]) -> str:
    """Precedence: valid explicit arg -> frontmatter category -> heuristic -> imported."""
    if explicit and explicit in KNOWN_CATEGORIES:
        return explicit
    fm = _frontmatter(text).get("category", "")
    if fm in KNOWN_CATEGORIES:
        return fm
    return _categorize(text)


def format_note(content: str, filename: str = "",
                category: Optional[str] = None,
                source: Optional[str] = None) -> Tuple[str, str, str]:
    """Return (category, slug, formatted_markdown). Pure — no filesystem.

    The seam for a future local-model pass: swap this function's body to have a
    local model pick the category / rewrite the note; callers are unaffected.
    """
    body_wo_fm = _strip_frontmatter(content or "")
    title = _title(body_wo_fm, filename)
    slug = slugify(title)
    cat = choose_category(content or "", category)
    fm_lines = [
        "---",
        f"title: {title}",
        f"category: {cat}",
        f"source: {source or 'ingest'}",
        f"date: {time.strftime('%Y-%m-%d')}",
        f"slug: {slug}",
        "---",
        "",
    ]
    body = body_wo_fm.lstrip("\n")
    # Ensure the body has an H1: the graph titles nodes from the first
    # '# heading', so a scraped note without one would otherwise show as its
    # slug instead of its real title. (When the title came FROM a body H1,
    # one already exists and we don't double it.)
    if not re.search(r"^#\s+", body, re.M):
        body = f"# {title}\n\n" + body
    return cat, slug, "\n".join(fm_lines) + body
