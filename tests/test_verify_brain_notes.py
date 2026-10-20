"""VERIFY-06 — brain-note recall proof for the 2 curated reference notes
(`awesome-ai-security`, `awesome-agent-skills`), split per D-05 into two tiers:

- FTS/lexical half (`-k fts`): a HARD gate, no `importorskip`. An index-less
  fixture vault (temp dir, no ``index_dir``) forces `PrecisionVaultProvider`'s
  deterministic stdlib lexical ranker (Pitfall 2 — a full-vault scan is bounded
  and unreliable, and the vector tier would otherwise win).
- Embedding half (`-k embed`): gated on `importorskip("fastembed")` per-test
  (never at module scope — that would also skip the FTS hard gate). Asserts
  both slugs are present in the already-rebuilt `brains/llm-council/index/meta.json`
  (json-parsed, never `grep -c` — Pitfall 1) plus a fixture `build_index` +
  `BrainIndex.search` mechanism proof independent of the global index.
"""
import asyncio
import importlib.util
import json
import pathlib

import pytest

spec = importlib.util.spec_from_file_location(
    "precision_vault", pathlib.Path("providers/precision_vault.py"))
pv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pv)

# Real notes on this machine's vault (verified present this session). Read
# directly if available; fall back to a synthesized note sharing the same
# distinctive intro line so the fixture is portable to a machine without
# C:/dev/vault (e.g. CI).
_SOURCE_VAULT = pathlib.Path("C:/dev/vault")
_SECURITY_NOTE = _SOURCE_VAULT / "knowledge" / "security" / "awesome-ai-security.md"
_AGENTSDK_NOTE = _SOURCE_VAULT / "knowledge" / "agentsdk" / "awesome-agent-skills.md"

_FALLBACK_SECURITY = """---
title: Awesome AI Security — curated frameworks, standards & tools
tags: [moc, knowledge/security, reference, ai-security]
---

> Curated reference list ingested into the brain. Part of [[_index]].

A curated list of awesome AI security frameworks, standards, learning
resources, red teaming tools, and guardrails for agentic AI systems.
"""

_FALLBACK_AGENT_SKILLS = """---
title: Awesome Agent Skills — curated agent-skill resources
tags: [moc, knowledge/agentsdk, reference, agent-skills]
---

> Curated reference list ingested into the brain. Part of [[_topology]].

A curated list of awesome agent skills, tools, and resources for building
Claude Code and agentic AI skill ecosystems.
"""

# Shared keyword query — both real notes open their body with the identical
# "Curated reference list ingested into the brain." line, so this query
# overlaps both docs' lexical terms deterministically.
_SHARED_QUERY = "curated reference list ingested brain"


def _read_or_fallback(path: pathlib.Path, fallback: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return fallback


def _fixture_vault(tmp_path: pathlib.Path) -> pathlib.Path:
    """Seed a temp vault with the 2 real notes (or synthesized fallbacks) under
    knowledge/security/ + knowledge/agentsdk/ — never the stale
    brains/llm-council/vault/ subset (T-09-07)."""
    vault = tmp_path / "vault"
    security_dir = vault / "knowledge" / "security"
    agentsdk_dir = vault / "knowledge" / "agentsdk"
    security_dir.mkdir(parents=True)
    agentsdk_dir.mkdir(parents=True)
    (security_dir / "awesome-ai-security.md").write_text(
        _read_or_fallback(_SECURITY_NOTE, _FALLBACK_SECURITY), encoding="utf-8")
    (agentsdk_dir / "awesome-agent-skills.md").write_text(
        _read_or_fallback(_AGENTSDK_NOTE, _FALLBACK_AGENT_SKILLS), encoding="utf-8")
    return vault


def test_notes_recall_fts_both_present(tmp_path):
    """HARD gate (no skip): index-less PrecisionVaultProvider over a fixture
    vault recalls both notes by keyword via the deterministic lexical ranker."""
    vault = _fixture_vault(tmp_path)
    # index_dir=None forces the lexical path — no vector index to short-circuit it.
    provider = pv.PrecisionVaultProvider(vault_path=str(vault), index_dir=None)
    hits = asyncio.run(provider.recall(_SHARED_QUERY, top_k=5))
    slugs = {pathlib.Path(h.memory.id).stem for h in hits}
    assert "awesome-ai-security" in slugs
    assert "awesome-agent-skills" in slugs


# Path to the real, already-rebuilt embedding index (reindex ran 2026-07-04:
# 116,447 rows @ dim 384, both target slugs present). Read-only here — no
# reindex is triggered, no mutation of the shipped artifact (T-09-08).
_REAL_META_PATH = pathlib.Path("brains/llm-council/index/meta.json")


def test_notes_in_embedding_index_embed():
    """Both slugs are present in the rebuilt global index. Parsed with `json`
    (never `grep -c` — meta.json is single-line JSON, Pitfall 1). Read-only
    presence assertion; the reindex precondition already ran, so this never
    triggers a rebuild."""
    pytest.importorskip("fastembed", reason="embedding tier requires fastembed")
    if not _REAL_META_PATH.is_file():
        pytest.skip("machine-local brain index is not present; fixture index test covers the mechanism")
    meta = json.loads(_REAL_META_PATH.read_text(encoding="utf-8"))
    ids = {row["id"] for row in meta["meta"]}
    assert "awesome-ai-security" in ids
    assert "awesome-agent-skills" in ids


def test_notes_fixture_index_embed(tmp_path):
    """Mechanism proof independent of the global index: build_index over a
    fixture vault seeded with the 2 real notes, then BrainIndex.search returns
    both. Writes only under tmp_path — the global index is never touched."""
    pytest.importorskip("fastembed", reason="embedding tier requires fastembed")
    import src.brain_index as bi

    vault = _fixture_vault(tmp_path)
    out_dir = tmp_path / "index"
    bi.build_index(vault, out_dir)
    idx = bi.BrainIndex(out_dir)
    hits = idx.search(_SHARED_QUERY, top_k=5)
    slugs = {h["id"] for h in hits}
    assert "awesome-ai-security" in slugs
    assert "awesome-agent-skills" in slugs
