# Bash — language grounding

When reviewing Bash, weigh these domain specifics:

- **Unquoted variables**: `rm -rf $dir` is catastrophic if `$dir` is empty/has
  spaces. Always quote: `"$dir"`. Prefer `set -euo pipefail`.
- **Word splitting / globbing**: quote expansions; use arrays for lists.
- **Command injection**: never `eval` or interpolate untrusted input into a
  command; validate/whitelist.
- **Error handling**: check exit codes; `set -e` doesn't catch everything
  (pipes, subshells) — use `pipefail` and explicit checks.
- **Temp files**: `mktemp`, clean up with `trap ... EXIT`; predictable names are
  a race/security risk.
- **Portability**: `#!/usr/bin/env bash`; beware `sh` vs `bash`-isms.
