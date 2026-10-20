"""Live integration checks for the remaining non-Query surfaces."""

import asyncio
import importlib.util
import pathlib
from unittest.mock import MagicMock


def test_skill_loader_invokes_live_parse_skill(monkeypatch):
    import src.skill_loader as skill_loader

    spy = MagicMock(wraps=skill_loader.parse_skill)
    monkeypatch.setattr(skill_loader, "parse_skill", spy)

    skills = skill_loader.discover_skills(".claude/skills")

    assert spy.called
    assert spy.call_count == len(skills) == 27


_spec = importlib.util.spec_from_file_location(
    "precision_vault",
    pathlib.Path("providers/precision_vault.py"),
)
_pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pv)

_NOTE_BODY = (
    "---\ntitle: Awesome AI Security\n---\n\n"
    "Curated reference list ingested into the brain about agent security.\n"
)


def _fixture_vault(tmp_path: pathlib.Path) -> pathlib.Path:
    vault = tmp_path / "vault"
    (vault / "knowledge" / "security").mkdir(parents=True)
    (vault / "knowledge" / "security" / "awesome-ai-security.md").write_text(
        _NOTE_BODY,
        encoding="utf-8",
    )
    return vault


def test_precision_vault_recall_invoked_for_brain_note_query(tmp_path):
    vault = _fixture_vault(tmp_path)
    provider = _pv.PrecisionVaultProvider(vault_path=str(vault), index_dir=None)

    spy = MagicMock(wraps=provider.recall)
    provider.recall = spy

    hits = asyncio.run(
        provider.recall("curated reference security agent", top_k=5)
    )

    assert spy.called
    assert hits
    assert any("awesome-ai-security" in str(hit.memory.id) for hit in hits)
