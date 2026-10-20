# v2.0 Test Baseline

**Invariant: every non-pass in this suite is EITHER a documented capability-skip (see Skip Ledger below) OR a named root-cause-pending known-red (see Known-Reds below). There are zero unexplained failures.**

This file is the milestone's honest, reproducible test baseline, recorded at the end of Phase 33 (Tech-Debt Burn-Down & Clean Test Baseline), the final phase of the v2.0 milestone. It is the number the v2.0 milestone audit trusts.

## How to Reproduce

```
python -m pytest tests/ -q -rs -p no:cacheprovider
```

**Canonical interpreter (research A2):** whichever `python` resolves first on this machine's `PATH`. On this execution machine that resolves to `C:\Users\example\AppData\Local\Microsoft\WindowsApps\python.exe`, reporting **Python 3.14.3** (pytest 9.1.1, pytest-asyncio 1.4.0, ruff 0.15.15).

**Documented deviation:** the project's declared interpreter is Python 3.12 (see `.claude/CLAUDE.md` — "Python 3.12+"), and this repo also carries a `venv/` pinned to **Python 3.11.9**. Neither the declared 3.12 nor the local `venv`'s 3.11.9 is what actually resolves first on this machine's `PATH` at execution time — 3.14.3 does. Per research Assumption A2, this is accepted as the canonical interpreter for this baseline because it reproduces the Phase 28-32 historical pass/skip/fail count **byte-for-byte** (3252 passed / 122 skipped / 0 failed, matching 33-08's plan-end verification exactly, run-to-run, on two independent invocations in this same session — see 33-09 Task 3 below). If a future execution machine resolves a different canonical interpreter (e.g., CI running the declared 3.12), the exact counts should be re-confirmed; the underlying fixes (pollution-teardown patterns, skip predicates) are Python-version-independent per research.

## Recorded Counts (fresh run, this session)

```
3252 passed, 122 skipped, 41 warnings in 275.01s (0:04:35)
```

- **Passed:** 3252
- **Skipped:** 122 (every one accounted for below — Skip Ledger)
- **Failed:** 0
- **Unexplained failures:** 0

This is byte-identical to 33-08-SUMMARY.md's plan-end full-suite verification (`3252 passed, 122 skipped, 0 failed` in 261s), confirming the baseline is stable across the Wave-2 → Wave-3 boundary with no regression introduced by Phase 33's own skip-marking (33-06) or documentation work.

**Reconciliation — committed-tree fresh run yields 3260, not 3252.** The 3252 figure above was measured in 33-09 Task 1 *before* this same plan added `tests/test_phase33_baseline.py` (the 8 SC-5 baseline-checker tests). A fresh run of the current committed tree therefore reports **3260 passed** = 3252 product-suite tests + 8 SC-5 meta-tests (skipped 122 and failed 0 are unchanged). This was confirmed by the 33-09 Task 3 trust smoke (`3260 passed / 122 skipped / 0 failed`). The 3252 headline characterizes the product suite at the moment the DEBT-01/02/03 work completed; 3260 is what you will see running the reproduce command below. Both are the same zero-unexplained-failure state.

## DEBT-01 / DEBT-02 Confirmations (from 33-01-SUMMARY.md, re-verified on-machine)

- **DEBT-01** (15-file query-builder named suite, incl. both `test_query_routes_registered` tests): `python -m pytest tests/test_query_catalog.py tests/test_query_compiler.py tests/test_query_compiler_audit.py tests/test_query_compiler_ui_key.py tests/test_query_external.py tests/test_query_owner_scoping.py tests/test_query_privilege.py tests/test_query_routes.py tests/test_query_routes_external.py tests/test_query_routes_update.py tests/test_query_routes_registered.py tests/test_querybuilder_models.py tests/test_tool_run_query.py tests/test_verify_qb_query_access_integration.py tests/test_verify_qb_spine_integration.py -q` → **99 passed**, 5 warnings, 27.45s. Byte-identical to research HEAD `d729751`'s recorded 99/99.
- **DEBT-02** (ruff F841 + full lint clean): `ruff check --select F841 .` → "All checks passed!"; `ruff check .` → "All checks passed!". All three candidate production-file F841 per-file-ignores (`mcp_servers/email_server.py`, `routes/model_routes.py`, `src/document_actions.py`) audited and confirmed still load-bearing (each still reports exactly one real F841 finding with the ignore removed) — none dropped, `pyproject.toml` unchanged.

## Skip Ledger

Every one of the 122 skips below is a documented capability/platform-availability skip — none is a disguised failure. Grouped by predicate/reason.

| Reason (skipif predicate / importorskip target) | Count | Representative file(s) |
|---|---|---|
| JS harness needs a compatible Node (`RUN_JS_TESTS` env var unset; app JS runs in-browser, not under this Python harness) | 90 | `test_calendar_utils_dates_js.py`, `test_censor_pref_js.py`, `test_compare_js.py`, `test_cookbook_progress_signal_js.py`, `test_email_linkify_security_js.py`, `test_esc_menu_stack_js.py`, `test_gmail_quote_attribution_js.py`, `test_hex_to_rgb_js.py`, `test_keybind_altgr_js.py` (13), `test_lang_icon_null_opts_js.py`, `test_local_endpoint_api_key_js.py`, `test_local_endpoint_js.py` (12), `test_markdown_rendering_js.py`, `test_markdown_table_row_js.py`, `test_match_model_key_js.py`, `test_model_sort_js.py`, `test_ordinal_suffix_js.py`, `test_popup_opener_isolation_js.py`, `test_preset_local_storage_js.py`, `test_providers_mixtral_logo_js.py`, `test_reply_all_cc_nonstring_js.py`, `test_reply_recipients_js.py`, `test_signature_fold_js.py`, `test_signature_fold_self_closing_br_js.py`, `test_snap_other_layers_nonarray_js.py` |
| macOS-only test on a non-macOS host (`sys.platform != "darwin"`) | 9 | `test_hwfit_macos.py` |
| could not import `openpyxl` (`importorskip("openpyxl")`) — optional Office-file dependency not installed | 4 | `test_datasource_files.py` |
| WSL bash-launcher command-substitution returns empty on Windows (`sys.platform == "win32"`) — added 33-06 (DEBT-03) | 4 | `test_cookbook_helpers.py` |
| POSIX file modes only (`sys.platform != "win32"` / equivalent) — pre-existing, NTFS has no POSIX mode bits | 3 | `test_api_key_manager_key_perms.py` (2), `test_api_key_manager_sc_proof.py` (1) |
| POSIX resource module only (`sys.platform == "win32"`, `resource` module import guard) — pre-existing | 3 | `test_council_verify.py` (2), `test_verify_cross_cutting_hardening_integration.py` (1) |
| POSIX-only `preexec_fn` wiring (`sys.platform == "win32"`) — pre-existing | 2 | `test_council_verify.py` |
| WSL bash-launcher PATH resolution mangles native Windows paths (`sys.platform == "win32"`) — added 33-06 (DEBT-03) | 2 | `test_amd_gpu_check_args.py` |
| POSIX file mode bits not meaningful on Windows (`sys.platform == "win32"` / `os.name == "nt"`) | 2 | `test_odysseus_dispatcher.py` (1, added 33-06/DEBT-03), `test_secret_storage_key_perms.py` (1, pre-existing) |
| could not import `markitdown` (`importorskip("markitdown")`) — optional Office/EPUB extraction dependency not installed | 1 | `test_markitdown_runtime.py` |
| Windows VT-mode enabling requires a genuine attached console (`os.name == "nt"`) — added 33-06 (DEBT-03) | 1 | `test_pr_blocker_audit.py` |
| POSIX mode bits (0o600) don't exist on Windows; key file protected by user-profile NTFS ACL instead, `safe_chmod` no-ops there | 1 | `test_security_regressions.py` |

**Total: 122.** (90 + 9 + 4 + 4 + 3 + 3 + 2 + 2 + 2 + 1 + 1 + 1 = 122.)

Of these, **8** (`test_amd_gpu_check_args.py` ×2, `test_cookbook_helpers.py` ×4, `test_odysseus_dispatcher.py` ×1, `test_pr_blocker_audit.py` ×1) were newly added this phase (33-06, DEBT-03) converting an honest FAIL into an honest SKIP with an exact capability predicate. The remaining 114 pre-date this phase (JS-harness-needs-Node, POSIX-only, missing-optional-dependency skips) and were already clean/honest before Phase 33 began.

## Known-Reds

**None — all resolved.** The original 34-failure environment baseline (Phases 28-32) has been fully closed:
- 32 items were fixed or converted to documented capability-skips across 33-02 through 33-08.
- The 2 residual root-cause-pending pollution items research had flagged as acceptable "known-reds" (`test_model_routes.py::test_llm_core_list_model_ids_uses_cached_configured_proxy` — #33, and `test_webhook_ssrf_resilience.py::test_webhook_delivery_uses_naive_utc_timestamps` — #34) were bisected and fixed in 33-08, along with 6 additional `sys.modules` pollution sources discovered during that bisection (also fixed, same defect class).

**0 known-reds remain in this baseline.** Every one of the 122 recorded skips is a capability/platform skip (Skip Ledger above); 0 tests are failing for an unresolved reason.

## Order-Stability (proven pairs)

The following previously-polluted file pairs are proven order-stable (each target passes both standalone and when run together with its former polluter) — asserted by `tests/test_phase33_baseline.py` (SC-5):

- **Pattern-1 pair** (33-02): `tests/test_ide_token.py` + `tests/test_image_gen_server_data_dir.py` + `tests/test_phase26_integration.py`
- **Pattern-2 pair** (33-03/33-08): `tests/test_auth_config_lock_concurrency.py` + `tests/test_tool_run_query.py` + `tests/test_verify_qb_spine_integration.py`
