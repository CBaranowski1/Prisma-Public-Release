"""Regressions for hybrid brain retrieval over stale semantic vectors.

These tests use fixed vectors and a fake embedder so they are deterministic,
offline, and do not require fastembed.  They reproduce the production failure
where an unrelated VERA profile scored 1.0 while imported architecture
documents containing the exact query phrase were left at cosine score 0.0.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import numpy as np

import src.brain_index as brain_index
import src.embeddings as embeddings
from routes import vera_agents_routes


_PROVIDER_SPEC = importlib.util.spec_from_file_location(
    "hybrid_precision_vault",
    Path("providers/precision_vault.py"),
)
precision_vault = importlib.util.module_from_spec(_PROVIDER_SPEC)
_PROVIDER_SPEC.loader.exec_module(precision_vault)


class _FixedEmbedder:
    url = "fake://hybrid-retrieval"
    model = "fixed"

    def encode(self, texts, normalize_embeddings=True):
        return [[1.0, 0.0] for _ in texts]


def _index_with_stale_vectors(tmp_path, monkeypatch):
    idx = brain_index.BrainIndex(tmp_path / "index")
    idx._main_vecs = np.asarray(
        [
            [1.0, 0.0],  # unrelated profile: misleading perfect semantic score
            [0.0, 1.0],  # imported AI-SPEC: stale/incorrect semantic score
            [0.0, 1.0],  # imported research: stale/incorrect semantic score
        ],
        dtype=np.float32,
    )
    idx._main_meta = [
        {
            "id": "architecture-advisor",
            "category": "vera-agents",
            "title": "Architecture Advisor",
            "excerpt": "A general VERA specialist profile.",
        },
        {
            "id": "AI-SPEC",
            "category": "imported",
            "title": "AI Specification",
            "excerpt": "Prisma frontier architecture contract and implementation guidance.",
        },
        {
            "id": "RESEARCH",
            "category": "imported",
            "title": "Frontier Model Research",
            "excerpt": "Evidence for the Prisma frontier architecture contract.",
        },
    ]
    monkeypatch.setattr(embeddings, "get_embedding_client", lambda **_kwargs: _FixedEmbedder())
    return idx


def test_exact_document_phrase_beats_stale_perfect_vector(tmp_path, monkeypatch):
    idx = _index_with_stale_vectors(tmp_path, monkeypatch)

    hits = idx.search("Prisma frontier architecture contract", top_k=2)

    assert [hit["id"] for hit in hits] == ["AI-SPEC", "RESEARCH"]
    assert all(hit["score"] == 1.0 for hit in hits)


def test_weak_lexical_overlap_does_not_displace_strong_semantic_hit(tmp_path, monkeypatch):
    idx = brain_index.BrainIndex(tmp_path / "index")
    idx._main_vecs = np.asarray(
        [
            [0.9, 0.4358899],
            [0.0, 1.0],
        ],
        dtype=np.float32,
    )
    idx._main_meta = [
        {
            "id": "strong-semantic",
            "category": "reference",
            "title": "Distributed systems design",
            "excerpt": "A semantically relevant treatment of the requested subject.",
        },
        {
            "id": "weak-lexical",
            "category": "reference",
            "title": "Frontier notes",
            "excerpt": "Only one query word overlaps this otherwise unrelated note.",
        },
    ]
    monkeypatch.setattr(embeddings, "get_embedding_client", lambda **_kwargs: _FixedEmbedder())

    hits = idx.search("frontier pipeline architecture", top_k=2)

    assert hits[0]["id"] == "strong-semantic"
    assert hits[0]["semantic_score"] > hits[1]["semantic_score"]


def test_vera_profile_scores_are_query_coverage_not_raw_match_counts(monkeypatch):
    monkeypatch.setattr(
        vera_agents_routes,
        "_cached",
        lambda: {
            "agents": [
                {
                    "id": "architecture-advisor",
                    "name": "Architecture Advisor",
                    "role": "advisor",
                    "squad": "design",
                    "brain": "vera-brain",
                    "activation_tokens": [],
                }
            ]
        },
    )

    rows = vera_agents_routes.catalog_documents(
        "Prisma frontier architecture contract",
        limit=20,
    )

    assert len(rows) == 1
    assert rows[0]["score"] == 0.25


def test_indexless_lexical_search_preserves_normalized_relevance(tmp_path):
    vault = tmp_path / "vera-brain"
    imported = vault / "knowledge" / "imported"
    profiles = vault / "knowledge" / "profiles"
    imported.mkdir(parents=True)
    profiles.mkdir(parents=True)
    (imported / "AI-SPEC.md").write_text(
        "# AI Specification\n\nPrisma frontier architecture contract and guidance.",
        encoding="utf-8",
    )
    (imported / "RESEARCH.md").write_text(
        "# Research\n\nEvidence for the Prisma frontier architecture contract.",
        encoding="utf-8",
    )
    (profiles / "architecture-advisor.md").write_text(
        "# Architecture Advisor\n\nA general VERA specialist profile.",
        encoding="utf-8",
    )
    provider = precision_vault.PrecisionVaultProvider(
        vault_path=str(vault),
        index_dir=str(vault),  # configured, but deliberately absent/stale
    )

    docs = asyncio.run(
        provider.search("Prisma frontier architecture contract", limit=3)
    )

    assert {doc.title for doc in docs[:2]} == {"Ai Spec", "Research"}
    assert [doc.score for doc in docs[:2]] == [1.0, 1.0]
    assert docs[2].score == 0.25
