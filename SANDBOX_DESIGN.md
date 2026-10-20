# Agent Shell/Filesystem Sandbox — Design & Interim Guardrail

Status: design + minimal guardrail shipped. Full sandbox tracked in #1058.
Audit reference: THREAT_MODEL.md "Known Gaps" #1.

## Problem

The agent exposes code-execution tools whose blast radius is the whole app-process user:

- `bash` — `asyncio.create_subprocess_shell(content, cwd=_AGENT_WORKDIR)` runs an
  arbitrary shell command (`src/tool_execution.py`, `run_tool` bash branch).
- `python` — `create_subprocess_exec(sys.executable, "-I", "-c", content)` runs
  arbitrary Python (same file, python branch).

Neither has **network-egress filtering** or **filesystem confinement**. They inherit
the process environment and run as the same OS user as the server, so a successful
prompt-injection that reaches a shell-enabled admin session can:

- read/write any file the server user can (SSH keys, `.env`, `data/auth.json`,
  `data/sessions.json`);
- make outbound requests to internal services (ChromaDB, Ollama, SearXNG, the
  cloud metadata endpoint on a cloud host) — the same class of target the webhook
  SSRF guard blocks, but here via a shell, not an HTTP URL field.

### What is already confined (do not duplicate)

- `read_file` / `write_file` / code-nav (`grep`/`glob`/`ls`) resolve model-supplied
  paths through an allowlist + sensitive-file policy and reject traversal
  (`src/tool_execution.py` path-confinement block; `tests/test_tool_path_confinement.py`,
  `tests/test_personal_dir_symlink_escape.py`). The gap is specifically the shell/interpreter tools.
- Tool authorization already fails closed for non-admins: `bash`/`python` are in
  `NON_ADMIN_BLOCKED_TOOLS` and blocked at execution (`src/tool_security.py`,
  `src/tool_execution.py`). So the exposure is **admin sessions reached via
  prompt-injection**, not ordinary non-admin users.

## Interim guardrail (shipped)

`PRISM_AGENT_SHELL_DISABLED` (default `false`). When truthy, the `bash` and `python`
tool branches return a policy error (`exit_code 126`) instead of spawning a process
(`_agent_shell_disabled()` in `src/tool_execution.py`). This gives operators in
shared or internal-network deployments a kill-switch for the shell surface without
waiting for the full sandbox, and is a no-op by default so single-user setups are
unchanged.

This is a mitigation, not the fix — it removes the capability rather than confining
it. Agents that need shell/python still run unconfined when the flag is off.

## Proposed sandbox (full fix)

Goal: run `bash`/`python` tool bodies in a jailed child that keeps the useful
capability (run code in the agent workdir) while removing ambient filesystem and
network authority. Defense-in-depth layered so a miss in one layer is not fatal.

1. **Process isolation (Linux baseline): `bubblewrap`.**
   Wrap the command: `bwrap --unshare-all --die-with-parent --ro-bind /usr /usr
   --bind <workdir> <workdir> --chdir <workdir> --proc /proc --dev /dev -- <cmd>`.
   `--unshare-all` drops the network namespace (no egress) and PID/IPC/UTS; only
   the agent workdir is bind-mounted writable, `/usr` read-only. Detect at startup;
   fall back to the kill-switch (deny) when `bwrap` is absent rather than running
   unconfined.

2. **Network egress: deny by default.**
   The unshared net namespace already yields no interfaces. For agents that must
   fetch, provide an explicit opt-in proxy allowlist (reuse the
   `is_public_http_url` / private-range logic from `src/url_security.py` and
   `src/webhook_manager.py`) so internal services and the metadata IP stay
   unreachable even when egress is enabled.

3. **Filesystem: bind only the workdir.**
   Read-only OS/runtime, read-write only `_AGENT_WORKDIR`. `data/` (auth.json,
   sessions.json, vault, uploads) is never bound in, so a shell cannot read
   credentials or the token store.

4. **Resource limits.** `rlimit`/cgroup caps on CPU, memory, PIDs, and file size
   layered on the existing wall-clock timeout (`DEFAULT_BASH_TIMEOUT`) to bound
   fork bombs and disk-fill.

5. **Cross-platform.**
   - macOS: `sandbox-exec` with a profile (deny network + file-write outside the
     workdir).
   - Windows: no cheap equivalent; ship the deny kill-switch as the supported
     posture and document that shell tools require the container image for
     confinement.
   - Docker: the container is already an isolation boundary; the compose stack
     binds services to loopback, so document the container as the recommended
     confinement and still apply bubblewrap inside it for defense-in-depth.

## Rollout

1. Kill-switch (done) — operators can disable today.
2. `bubblewrap` wrapper behind `PRISM_AGENT_SANDBOX=bwrap` (opt-in), default off;
   graduate to on-by-default once the test matrix is green.
3. Egress allowlist proxy for the fetch use-case.
4. macOS `sandbox-exec` profile; Windows stays deny-only outside a container.

## Test plan

- Injection regression: a `bash` body attempting `curl http://169.254.169.254/`,
  reading `../data/auth.json`, and writing outside the workdir must all fail under
  the sandbox.
- Kill-switch: with `PRISM_AGENT_SHELL_DISABLED=true`, `bash`/`python` return the
  policy error and spawn no process (unit test).
- No-regression: with both flags off, existing tool-execution tests stay green.
