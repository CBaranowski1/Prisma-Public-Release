# Project skills

Engineering agent skills available to Claude Code / GSD when working in this repo.

## Sources
- 27 skills from mattpocock/skills — "Skills For Real Engineers" (MIT): code-review,
  tdd, diagnosing-bugs, codebase-design, implement, research, handoff,
  resolving-merge-conflicts, improve-codebase-architecture, domain-modeling, triage,
  to-prd, to-issues, and more. The deprecated/ (4) and in-progress/ (7) skills were
  intentionally NOT imported (upstream marks them not-ready).

Two companion reference lists went into the second brain (knowledge, not capabilities):
- awesome-ai-security  -> vault/knowledge/security/awesome-ai-security.md
- awesome-agent-skills -> vault/knowledge/agentsdk/awesome-agent-skills.md

## How they load
Each skill is a <name>/SKILL.md dir with name+description frontmatter — the standard
format Claude Code and GSD read from .claude/skills/. Available automatically in
sessions launched from this project directory.
