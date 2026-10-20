"""Bundled skill assets and the unified runtime registry."""

from pathlib import Path
import pytest

from src.skill_loader import discover_skills, parse_skill

SKILLS_DIR = ".claude/skills"

# Explicit allowlist (Pitfall 4 — guards silent count drift; a bare len()==27
# assertion would not notice a skill swapped for another of the same count).
EXPECTED_SKILLS = {
    "ask-matt",
    "code-review",
    "codebase-design",
    "diagnosing-bugs",
    "domain-modeling",
    "edit-article",
    "git-guardrails-claude-code",
    "grill-me",
    "grill-with-docs",
    "grilling",
    "handoff",
    "implement",
    "improve-codebase-architecture",
    "migrate-to-shoehorn",
    "obsidian-vault",
    "prototype",
    "research",
    "resolving-merge-conflicts",
    "scaffold-exercises",
    "setup-matt-pocock-skills",
    "setup-pre-commit",
    "tdd",
    "teach",
    "to-issues",
    "to-prd",
    "triage",
    "writing-great-skills",
}


def test_docker_context_includes_only_bundled_skill_tree():
    """The image must receive bundled skills without exposing .claude state."""
    lines = [
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    # Excluding the parent directory wholesale prevents Docker from walking
    # down to a later negation, so the root rule must exclude its children
    # instead and then re-include only the curated skill subtree.
    assert ".claude/" not in lines
    root_exclude = lines.index(".claude/*")
    skill_dir_include = lines.index("!.claude/skills/")
    skill_tree_include = lines.index("!.claude/skills/**")
    assert root_exclude < skill_dir_include < skill_tree_include
    assert lines.index("*.md") < skill_dir_include
    assert lines[-2:] == ["!.claude/skills/", "!.claude/skills/**"]

    claude_exceptions = {line for line in lines if line.startswith("!.claude")}
    assert claude_exceptions == {
        "!.claude/skills/",
        "!.claude/skills/**",
    }
    # User data and secrets remain outside the build context.
    assert "/data/" in lines
    assert ".env" in lines


def test_discovers_exactly_27_named_skills():
    skills = discover_skills(SKILLS_DIR)
    discovered = {s["dir"] for s in skills}
    assert len(EXPECTED_SKILLS) == 27
    assert discovered == EXPECTED_SKILLS
    assert len(skills) == 27


def test_unified_registry_exposes_27_bundled_plus_2_owner_skills():
    """Chat/Agent/Council share one 29-package logical registry.

    Bundled procedures are visible to every owner, while the two writable
    Prisma procedures remain visible only to their declared owner.
    """
    from services.memory.skills import SkillsManager

    manager = SkillsManager(str(Path("data").resolve()))
    admin_names = {s["name"] for s in manager.index_for(owner="admin")}
    other_names = {s["name"] for s in manager.index_for(owner="another-user")}

    assert admin_names == EXPECTED_SKILLS | {
        "second-brain-operator",
        "vera-agent-catalog",
    }
    assert other_names == EXPECTED_SKILLS
    assert manager.read_skill_md("code-review", owner="another-user")
    assert manager.read_skill_md("second-brain-operator", owner="another-user") is None


def test_private_override_does_not_hide_bundled_skill_from_other_owner(tmp_path):
    from services.memory.skills import SkillsManager

    bundled_dir = tmp_path / "bundled"
    bundled_skill = bundled_dir / "code-review" / "SKILL.md"
    bundled_skill.parent.mkdir(parents=True)
    bundled_skill.write_text(
        "---\nname: code-review\ndescription: bundled review\n---\nbundled body\n",
        encoding="utf-8",
    )
    manager = SkillsManager(
        str(tmp_path / "data"),
        bundled_skills_dir=str(bundled_dir),
    )
    private_skill = tmp_path / "data" / "skills" / "personal" / "code-review" / "SKILL.md"
    private_skill.parent.mkdir(parents=True)
    private_skill.write_text(
        "---\n"
        "name: code-review\n"
        "description: alice override\n"
        "category: personal\n"
        "status: published\n"
        "owner: alice\n"
        "source: user\n"
        "---\n"
        "alice body\n",
        encoding="utf-8",
    )

    alice = [s for s in manager.load(owner="alice") if s["name"] == "code-review"]
    bob = [s for s in manager.load(owner="bob") if s["name"] == "code-review"]
    assert len(alice) == 1 and alice[0]["description"] == "alice override"
    assert len(bob) == 1 and bob[0]["description"] == "bundled review"
    assert "bundled body" in (manager.read_skill_md("code-review", owner="bob") or "")


def test_every_discovered_skill_is_well_formed():
    skills = discover_skills(SKILLS_DIR)
    for skill in skills:
        assert skill["name"], f"{skill['dir']}: empty name"
        assert skill["description"], f"{skill['dir']}: empty description"


def test_malformed_skill_md_raises_value_error(tmp_path):
    bad = tmp_path / "broken-skill" / "SKILL.md"
    bad.parent.mkdir()
    bad.write_text("no front matter here at all", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_skill(bad)


def test_empty_name_field_raises_value_error(tmp_path):
    # WR-01 regression: an empty `name:` must fail loud, NOT let the regex cross
    # the newline and capture the `description:` line as the name.
    bad = tmp_path / "empty-name" / "SKILL.md"
    bad.parent.mkdir()
    bad.write_text("---\nname:\ndescription: a skill\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_skill(bad)


def test_bom_authored_skill_md_parses(tmp_path):
    # WR-02 regression: a UTF-8 BOM at the start must not break the front-matter
    # match (which would otherwise abort discovery of every skill).
    skill = tmp_path / "bom-skill" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(
        "---\nname: bom-skill\ndescription: has a leading BOM\n---\nbody\n",
        encoding="utf-8-sig",
    )
    parsed = parse_skill(skill)
    assert parsed["name"] == "bom-skill"
    assert parsed["description"] == "has a leading BOM"
    # And discovery over the dir still finds it (list-comprehension not aborted).
    assert {s["dir"] for s in discover_skills(str(tmp_path))} == {"bom-skill"}


def test_dir_without_skill_md_is_not_counted(tmp_path):
    (tmp_path / "has-skill").mkdir()
    (tmp_path / "has-skill" / "SKILL.md").write_text(
        "---\nname: has-skill\ndescription: a skill\n---\nbody", encoding="utf-8"
    )
    (tmp_path / "readme-only").mkdir()
    (tmp_path / "readme-only" / "README.md").write_text("not a skill", encoding="utf-8")

    skills = discover_skills(str(tmp_path))
    assert {s["dir"] for s in skills} == {"has-skill"}


def test_load_skill_instructions_rejects_traversal(tmp_path):
    # Code-review WR (Phase 10): the docstring claims dir-confinement; enforce it.
    # A `..` name must not escape skills_dir — returns "" rather than reading outside.
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "SKILL.md").write_text(
        "---\nname: real\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    # plant a SKILL.md one level up that a `../` name could otherwise reach
    (tmp_path / "SKILL.md").write_text("---\nname: outside\ndescription: d\n---\nsecret\n", encoding="utf-8")
    from src.skill_loader import load_skill_instructions
    assert load_skill_instructions("../", str(tmp_path / "real")) == ""
    assert load_skill_instructions("real", str(tmp_path)) == "body"
