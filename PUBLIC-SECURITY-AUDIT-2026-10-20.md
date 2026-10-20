# Public repository security review — 2026-10-20

Scope: published `CBaranowski1/Prisma-Public-Release` source, the validated 1,371-file release snapshot, current GitHub branch metadata, public commit list, and GitHub Actions checks. **This report does not certify the safety of an internet-facing deployment.**

## What was verified

- The current public tree contains 1,369 tracked files; it has no tracked `.env`, database, raw vault knowledge corpus, private planning directory, or specialist private-brain configuration subtree.
- The source candidate's staged Git index contained **zero** files that matched its own `.gitignore` patterns. Publishing `.gitignore` itself is expected and not a leak.
- Independent text-pattern review found **no confirmed operational API key or database credential**. Key-shaped material exists in deliberately synthetic security tests; those test fixtures must continue to be reviewed by exact content and by Gitleaks.
- The release-candidate validation passed Python compilation, Ruff, JavaScript parsing, Bandit, a local FastAPI smoke test, and **3,462 passed / 129 skipped** tests. Its redacted Gitleaks scans reported no leaks.
- Public GitHub CI and redacted secret scanning have completed successfully on published commits. These tools cannot prove absence of every possible secret, personal datum, or malicious dependency.

## New live dependency-advisory results

The initial live `pip-audit` check identified **27 advisories across five packages**: `certifi`, `urllib3`, `mcp`, `PyJWT` and `pypdf`. A branch-only update refreshed `requirements.in`, `requirements.txt` and `requirements-dev.txt` with pinned, hashed patched versions. The lock regeneration job then reran `pip-audit` successfully with **no advisories reported for that production lock at audit time**. The JavaScript lock audit passed the high-severity threshold. These fixes are not on `main` until this pull request is merged and its full tests pass.

## Findings and actions

| Priority | Finding | Disposition |
| --- | --- | --- |
| High for deployment | The current `app.py` explicitly disables the login flow; administrative agents can access privileged local tools. | Documented; use loopback/private network plus authenticated gateway. Do **not** expose port 7000 unauthenticated. Full shell confinement is not proven. |
| Medium | Optional SparkLab Docker overlay contained machine-specific absolute mounts and a forced unauthenticated model configuration. | Portable, authenticated-by-default example proposed in public hygiene PR. This overlay is not required for the standard app. |
| Medium privacy | Public Git commit metadata uses a personal email address, instead of a GitHub-generated noreply address. | Still present in **past history**. A regular file edit cannot remove it; a carefully reviewed history rewrite and GitHub email-privacy setting change would be needed. Never mistake masking it in README for erasing history. |
| Low privacy | Generic adapter and tests preserve legacy specialist-project identifiers to avoid breaking compatibility. | No actual private vault content was confirmed in the exported tree; rename only after compatibility review. |
| Low/medium | A historical one-time import workflow contains a time-limited artifact-download URL pointing to the sanitized source candidate. | Import workflow removed from current tree. Historical commit remains accessible; link expiration should be verified. It does not appear to contain an operational provider secret. |
| Medium governance | Default public branch was observed without branch protection. | Enable protection / rulesets, require passing CI and scans, disallow force pushes and branch deletion. Needs repo administration. |
| Incomplete | Live Dependabot alert details, current dependency advisory state, full third-party redistribution licensing, PostgreSQL-specific E2E and native desktop packaging could not be exhaustively confirmed. | Supply-chain CI audit added. Review advisories and dependency licenses before claiming all-clear. |

## Recommended controls

1. Preserve `.gitignore` and `.dockerignore`; they do not prevent already-tracked secrets from being published, so continue full-history scanning.
2. Keep private knowledge, real models/weights, journals, logs, uploads and personal data outside Git.
3. Enable GitHub secret scanning, push protection, private vulnerability reporting, Dependabot alerts, branch rules and CodeQL wherever available.
4. Use GitHub noreply commits going forward; plan and approve any history rewrite separately.
5. Run the public CI, secret scan, and new supply-chain advisory jobs to completion. Investigate failures before merging.
6. For real deployments, require trusted access control, network isolation and tool privilege restrictions; never expose this no-login build directly to the internet.

**Verdict:** suitable for continued public-source review with the listed caveats. No confirmed live key leaked in the inspected snapshot. It is **not** certified production-safe or comprehensively vulnerability-free.
