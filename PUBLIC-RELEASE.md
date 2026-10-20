# Public release checklist (full source)

This project is intended to be published as **working source code**, not a
stub or a screenshot-only portfolio. Preserve all functional Python and
JavaScript modules, routes, services, tests, desktop sources, model
configuration templates, and other redistribution-cleared dependencies.

## Privacy boundary

The private repository is the source of truth. Publish a new clean-history
snapshot to a separate public repository, not the private historical Git log.

Remove only verified sensitive or non-redistributable material. Likely
review candidates include:
- `.planning/` (internal milestones, local machine paths, personal notes).
  Keep any docs that are necessary for functionality, relocated and sanitized.
- Runtime data, credentials, user sessions, personal logs and databases.
- `brains/precision-audit/` **only after** inspecting its provenance and any
  private-client content. Preserve functional configurations where permitted,
  replacing private identifiers with generic, documented defaults.
- Local hardware, hostname, IP, personal email, or filesystem values.
- Unlicensed third-party content and confidential customer information.

**Do not blanket-delete** the `brains/` hierarchy, tests, skills, core code,
integrations, or deployment recipes: such omissions may break the app.

## Gates

1. Inventory all tracked paths and review privacy and licensing.
2. Run `python scripts/public_release_check.py` against the exact export
   candidate. Resolve each failure by redaction or exclusion, without breaking
   application dependencies.
3. Run Gitleaks against candidate files and against its **new** one-commit
   history; never transplant canonical history.
4. Run Python syntax, Ruff, pytest, JavaScript syntax and dependency audits
   on the candidate. Compare test counts to the private baseline and
   investigate missing coverage.
5. Verify local install, first-run configuration, app startup, auth,
   privileged-tool restrictions and a minimal end-to-end workflow.
6. Review licensing and copyright notices. Check the public README's claims
   against the actual exported implementation.
7. Obtain owner approval for the exact release tree before publication.

A private CI green status is not proof that a derived public snapshot passed
these checks. Until the release is independently tested and a new public
repository created, the release status is **not shipped**.
