import asyncio, importlib.util, pathlib
import pytest
spec = importlib.util.spec_from_file_location("precision_vault",
    pathlib.Path("providers/precision_vault.py"))
pv = importlib.util.module_from_spec(spec); spec.loader.exec_module(pv)

VAULT = str(pathlib.Path("tests/fixtures/vault"))

def _p(): return pv.PrecisionVaultProvider(vault_path=VAULT)

def test_categories_from_folders():
    cats = asyncio.run(_p().categories())
    assert "postgres" in cats and "concepts" in cats

def test_search_lexical_overlap():
    docs = asyncio.run(_p().search("partition ingest retention"))
    assert docs and docs[0].title == "Postgres Partitioning"
    assert docs[0].category == "postgres"

def test_search_category_filter():
    docs = asyncio.run(_p().search("worker", category="concepts"))
    assert all(d.category == "concepts" for d in docs)

def test_recall_returns_hits():
    hits = asyncio.run(_p().recall("dedupe upserts", top_k=3))
    assert hits and "Idempotency" in hits[0].memory.text

def test_empty_query_lists_all_sorted_by_category():
    docs = asyncio.run(_p().search(""))
    # browse mode: the whole vault comes back (every category present)
    cats = [d.category for d in docs]
    assert "concepts" in cats and "postgres" in cats
    # grouped by category alphabetically (non-decreasing), titles sorted within
    assert cats == sorted(cats)
    for i in range(1, len(docs)):
        if docs[i].category == docs[i - 1].category:
            assert docs[i].title.lower() >= docs[i - 1].title.lower()

def test_empty_query_category_filter():
    docs = asyncio.run(_p().search("", category="postgres"))
    assert docs and all(d.category == "postgres" for d in docs)

def test_enabled_reflects_effective_path():
    # A provider with a usable vault path is enabled (property, not an init
    # snapshot of the env var) so a Settings-UI-configured vault isn't disabled.
    assert _p().enabled is True
    # No explicit path AND no vault that resolves -> disabled.
    assert pv.PrecisionVaultProvider(vault_path="")._explicit_path is None


def _fixture_long_body():
    return "Detailed idempotent retry design with exponential backoff and jitter tuning notes. " * 10


def test_recall_returns_full_body(tmp_path):
    pytest.importorskip("fastembed", reason="brain recall requires fastembed; pip install fastembed")
    import src.brain_index as bi

    vault = tmp_path / "vault"
    (vault / "knowledge" / "code").mkdir(parents=True)
    long_body = _fixture_long_body()
    (vault / "knowledge" / "code" / "long-note.md").write_text(
        "---\ntitle: Long Note\n---\n\n" + long_body, encoding="utf-8")

    brain_dir = tmp_path / "brain"
    bi.build_index(vault, brain_dir / "index")

    provider = pv.PrecisionVaultProvider(vault_path=str(vault), index_dir=str(brain_dir))
    hits = asyncio.run(provider.recall("idempotent retry design", top_k=3))
    assert hits
    text = hits[0].memory.text
    assert len(text) > 500
    assert long_body.strip()[-50:] in text


def test_recall_falls_back_to_excerpt_when_vault_file_missing(tmp_path):
    pytest.importorskip("fastembed", reason="brain recall requires fastembed; pip install fastembed")
    import src.brain_index as bi

    vault = tmp_path / "vault"
    (vault / "knowledge" / "code").mkdir(parents=True)
    long_body = _fixture_long_body()
    note_path = vault / "knowledge" / "code" / "long-note.md"
    note_path.write_text("---\ntitle: Long Note\n---\n\n" + long_body, encoding="utf-8")

    brain_dir = tmp_path / "brain"
    bi.build_index(vault, brain_dir / "index")

    # Simulate a vault file missing after indexing (D-03 silent-fallback path).
    note_path.unlink()

    provider = pv.PrecisionVaultProvider(vault_path=str(vault), index_dir=str(brain_dir))
    hits = asyncio.run(provider.recall("idempotent retry design", top_k=3))
    assert hits
    excerpt = " ".join(long_body.split())[:400]
    assert excerpt in hits[0].memory.text


def test_rehydrate_body_blocks_prefix_sibling_escape(tmp_path):
    # Regression for CR-01: the vault-root containment guard must reject a sibling
    # directory that shares the "knowledge" string prefix (e.g. ../knowledge_private),
    # which the previous startswith() check let through (ASVS V12, threat T-04-01).
    # Pure filesystem logic — no fastembed needed, so this always runs.
    vault = tmp_path / "vault"
    (vault / "knowledge" / "code").mkdir(parents=True)
    (vault / "knowledge" / "code" / "note.md").write_text(
        "---\ntitle: N\n---\nhello body", encoding="utf-8")
    secret_dir = vault / "knowledge_private"
    secret_dir.mkdir(parents=True)
    (secret_dir / "secret.md").write_text("TOPSECRET", encoding="utf-8")

    prov = pv.PrecisionVaultProvider(vault_path=str(vault))
    # A legitimate in-root note still re-hydrates (frontmatter stripped).
    assert prov._rehydrate_body("note", "code") == "hello body"
    # Prefix-sharing sibling escape is blocked — returns None, never reads the secret.
    assert prov._rehydrate_body("secret", "../knowledge_private") is None
    # Classic parent traversal is blocked too.
    assert prov._rehydrate_body("secret", "../../etc") is None
