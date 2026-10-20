"""Standalone SKILL.md discovery/validation loader.

Verifies that Claude Code / GSD skills under `.claude/skills/` are
discoverable and well-formed (VERIFY-05, D-03). This module reads and
regex-parses SKILL.md front-matter ONLY — it never `eval`/`exec`/`import`/
`compile`s skill body content (T-09-05), and it confines its directory walk
to the resolved `skills_dir` via a glob of immediate `*/SKILL.md` children,
with no user-supplied path traversal (T-09-06).

``discover_skills`` remains the lightweight verifier for one source tree.
``load_skill_instructions`` is the compatibility facade used by Council seats;
its default path now resolves through Prisma's unified ``SkillsManager`` so
Council and chat/Agent mode discover the same registry.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
# `[^\S\n]*` = horizontal whitespace only. A bare `\s*` would cross the newline
# after an empty `name:` field and let `(.+)` capture the NEXT front-matter line
# instead of failing (WR-01), silently mis-reporting a malformed skill as valid.
_NAME = re.compile(r"^name:[^\S\n]*(.+)$", re.M)
_DESCRIPTION = re.compile(r"^description:[^\S\n]*(.+)$", re.M)


def parse_skill(path: Path) -> Dict[str, str]:
    """Parse one SKILL.md front-matter block into name/description/dir.

    Raises ValueError if the front-matter block is absent or either
    `name:`/`description:` field is missing.
    """
    # utf-8-sig strips a leading BOM if present; a BOM left in place would break
    # the anchored `^---` front-matter match and, since discover_skills() is a
    # list comprehension, abort discovery of EVERY skill (WR-02).
    text = Path(path).read_text(encoding="utf-8-sig")
    match = _FRONT_MATTER.match(text)
    if not match:
        raise ValueError(f"{path}: missing front-matter")

    front_matter = match.group(1)
    name = _NAME.search(front_matter)
    description = _DESCRIPTION.search(front_matter)
    if not name or not description:
        raise ValueError(f"{path}: front-matter missing name/description")

    return {
        "name": name.group(1).strip(),
        "description": description.group(1).strip(),
        "dir": Path(path).parent.name,
    }


def discover_skills(skills_dir: str) -> List[Dict[str, str]]:
    """Discover and parse every SKILL.md under skills_dir's immediate subdirs.

    Only immediate `*/SKILL.md` children of the resolved skills_dir are
    walked; subdirs without a SKILL.md (e.g. README.md at the root) are
    skipped, not counted as skills.
    """
    resolved = Path(skills_dir).resolve()
    return [
        parse_skill(skill_md)
        for skill_md in sorted(resolved.glob("*/SKILL.md"))
    ]


def load_skill_instructions(
    name: str,
    skills_dir: Optional[str] = None,
    *,
    owner: Optional[str] = None,
    data_dir: Optional[str] = None,
) -> str:
    """Return a skill's SKILL.md body (everything after the front-matter).

    With an explicit ``skills_dir``, resolves by DIRECTORY name under that
    directory (Pitfall 4) — NOT by the
    front-matter `name:` field, so a directory/front-matter name divergence
    still resolves the correct directory's body. Confined to the immediate
    `<skills_dir>/<name>/SKILL.md` child, mirroring `discover_skills` (no
    traversal). Returns "" when that file does not exist.

    With the default ``skills_dir=None``, reads through ``SkillsManager``.
    That registry prefers owner-scoped writable packages and then falls back
    to immutable bundled Council packages. This is the production path.

    The body is returned as plain text ONLY — this function never
    eval/exec/import/compiles it (T-09-05 invariant, same as `parse_skill`).
    """
    if skills_dir is None:
        from services.memory.skill_format import parse_frontmatter
        from services.memory.skills import SkillsManager
        if data_dir is None:
            from src.constants import DATA_DIR
            data_dir = DATA_DIR
        text = SkillsManager(data_dir).read_skill_md(name, owner=owner)
        if text is None:
            return ""
        _fm, body = parse_frontmatter(text)
        return body.strip()

    root = Path(skills_dir).resolve()
    p = (root / name / "SKILL.md").resolve()
    # Confine to an immediate child of skills_dir — reject `..`/absolute-path
    # traversal in `name` (defence-in-depth; enforces the docstring's claim).
    if not p.is_relative_to(root) or not p.is_file():
        return ""
    text = p.read_text(encoding="utf-8-sig")
    match = _FRONT_MATTER.match(text)
    return text[match.end():].strip() if match else text.strip()
