"""Wave-0 test substrate for Phase 6 (chat-path-verified).

This file provides:
  1. A reusable async harness (`run_chat`) that drives the real
     `stream_agent_loop` streaming generator end-to-end under scripted LLM
     output, entirely offline (no live DB/MCP/network stack required). No
     existing test drives `stream_agent_loop` today -- `tests/test_agent_loop.py`
     only unit-tests pure helpers -- so this harness is built new.
  2. A smoke test proving the harness drives a plain-prose turn to completion.
  3. PRE-D-02 baseline characterization tests that pin TODAY's (unmodified)
     `_python_syntax_errors`-based syntax gate behavior, so Plan 02's D-02
     swap (replacing it with `verified_gate.evaluate`/`resolve`) has a
     non-regression anchor: a failing post-swap run is unambiguous evidence
     of Pitfall 2's regex-widening, not a pre-existing gap.

IMPORTANT (06-RESEARCH.md Pitfall 3): `"python"` is itself a member of
`TOOL_TAGS` (src/agent_tools.py:29) -- a literal ```python fenced block in
the model's answer is intercepted by `parse_tool_blocks` as a python-
EXECUTION tool call *before* the syntax gate branch (`if not tool_blocks:`)
is ever reached. Tests that want to exercise the answer-text syntax gate
MUST use the ```py alias or an UNTAGGED fence -- never a bare ```python
fence in isolation. Do not "fix" a test by switching it to ```python; that
silently changes which code path is under test (real execution, not the
gate).
"""

import json
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Import-stub dance (mirrors tests/test_agent_loop.py:7-47) -- but with a
# DIFFERENT stub set. Stub ONLY the heavy DB/IO layers so `src.agent_loop`
# can be imported and driven without a live database. `src.agent_tools` is
# kept REAL (NOT stubbed) -- the loop needs genuine `parse_tool_blocks`,
# `strip_tool_blocks`, `TOOL_TAGS`, `ToolBlock` to exercise the gate and the
# tool-block resolution path faithfully.
# ---------------------------------------------------------------------------
_MOCKED_IMPORTS = [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    'core.models', 'core.database',
]
_INJECTED_IMPORT_STUBS = {}
_PREEXISTING_AGENT_LOOP = sys.modules.get("src.agent_loop")


def _drop_module_if_same(name, expected):
    if sys.modules.get(name) is expected:
        sys.modules.pop(name, None)
    parent_name, _, attr = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, "__dict__", {}).get(attr) is expected:
        delattr(parent, attr)


# Mock heavy dependencies before importing. Only clean up stubs this file
# created so pre-existing conftest/pytest modules keep their intended state.
for _mod in _MOCKED_IMPORTS:
    if _mod not in sys.modules:
        _stub = MagicMock()
        sys.modules[_mod] = _stub
        _INJECTED_IMPORT_STUBS[_mod] = _stub

_IMPORTED_AGENT_LOOP = None
try:
    import src.agent_loop as agent_loop
    import src.settings as settings_module
    import src.tool_index as tool_index_module
    import src.verified_gate as verified_gate
    import src.tool_parsing as tool_parsing
    _IMPORTED_AGENT_LOOP = sys.modules.get("src.agent_loop")
finally:
    # NOTE: unlike tests/test_agent_loop.py (which only extracts a few pure
    # functions via `from src.agent_loop import (...)` and never touches the
    # module again), this file keeps a live `agent_loop` module reference and
    # monkeypatches ITS attributes directly in every test (never via a
    # string path like "src.agent_loop.x", which would force
    # `importlib.import_module` to RE-IMPORT a second, unpatched copy of the
    # module the moment it is missing from sys.modules -- exactly what the
    # cleanup below would otherwise cause). Only clean up the injected
    # heavy-dependency stubs so they don't leak into other test modules;
    # deliberately do NOT drop "src.agent_loop" itself.
    for _mod, _stub in _INJECTED_IMPORT_STUBS.items():
        _drop_module_if_same(_mod, _stub)


def test_import_stubs_do_not_leak_into_later_tests():
    # This file deliberately keeps "src.agent_loop" (and src.settings,
    # src.tool_index) live in sys.modules -- see the NOTE above the cleanup
    # block. Only the injected heavy-dependency stubs (sqlalchemy/core.*/
    # src.database, when this file was the one that first injected them)
    # must not leak into later test modules.
    leaked = [
        mod for mod, stub in _INJECTED_IMPORT_STUBS.items()
        if sys.modules.get(mod) is stub
    ]
    assert leaked == []


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _delta_chunks(text: str) -> list:
    """Build the SSE chunks for a single round: one text delta then [DONE].

    Chat's chunk shape is `data: {"delta": "..."}` -- NOT Council/Answer's
    plain-string `llm_call_async` return. Do not copy that harness's format.
    """
    return [f'data: {json.dumps({"delta": text})}\n\n', 'data: [DONE]\n\n']


def _tool_call_chunks(name: str, arguments: dict) -> list:
    """SSE chunks for a single round whose ONLY output is one native
    function-calling tool call -- routes through `_resolve_tool_blocks` ->
    `src.tool_schemas.function_call_to_tool_block`, the REAL conversion
    tool_schemas.py uses to build a ToolBlock's content per tool_type
    (agent_loop.py:1137-1163), exactly as production traffic does.

    Deliberately NOT a fenced ```tool_name block: a fenced representation
    cannot carry a body containing an embedded ``` fence without an
    ambiguous nested-fence parse (the outer fence's non-greedy close would
    match the INNER fence's opening ```), so this is the only unambiguous
    way to drive a doc-write body containing its own embedded fence through
    the real `stream_agent_loop` call path in this harness (gap-closure,
    Plan 09)."""
    payload = {"type": "tool_calls", "calls": [{"name": name, "arguments": json.dumps(arguments)}]}
    return [f'data: {json.dumps(payload)}\n\n', 'data: [DONE]\n\n']


def _extract_deltas(events: list) -> list:
    """Pull the ordered list of assistant text deltas out of an SSE stream."""
    out = []
    for e in events:
        if not e.startswith("data: ") or e.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(e[len("data: "):].strip())
        except json.JSONDecodeError:
            continue
        if "delta" in data:
            out.append(data["delta"])
    return out


def _agent_step_rounds(events: list) -> list:
    """Ordered list of `round` values from every `agent_step` SSE event."""
    out = []
    for e in events:
        if not e.startswith("data: ") or e.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(e[len("data: "):].strip())
        except json.JSONDecodeError:
            continue
        if data.get("type") == "agent_step":
            out.append(data["round"])
    return out


def _typed_events(events: list, type_name: str) -> list:
    """All parsed SSE event payloads whose `type` matches `type_name`."""
    out = []
    for e in events:
        if not e.startswith("data: ") or e.startswith("data: [DONE]"):
            continue
        try:
            data = json.loads(e[len("data: "):].strip())
        except json.JSONDecodeError:
            continue
        if data.get("type") == type_name:
            out.append(data)
    return out


# Wall-clock/perf fields that legitimately vary run-to-run even when the
# conversation shape (deltas, notes, event ordering) is identical -- must be
# normalized before a "byte-identical" comparison across two separate
# run_chat() invocations (UNIFY-02 (c)).
_VOLATILE_METRICS_KEYS = (
    "response_time", "time_to_first_token", "tokens_per_second",
    "agent_prep_time", "agent_model_wait_time", "agent_prep_breakdown",
)


def _normalize_events(events: list) -> list:
    """Re-serialize every SSE chunk with volatile timing fields zeroed out,
    so two separately-run turns can be compared for genuine behavioral
    (not timing-jitter) identity."""
    out = []
    for e in events:
        if not e.startswith("data: ") or e.startswith("data: [DONE]"):
            out.append(e)
            continue
        try:
            data = json.loads(e[len("data: "):].strip())
        except json.JSONDecodeError:
            out.append(e)
            continue
        if data.get("type") in ("metrics", "agent_prep") and isinstance(data.get("data"), dict):
            for key in _VOLATILE_METRICS_KEYS:
                if key in data["data"]:
                    data["data"][key] = 0
            for timing_key in ("request_setup", "tool_selection", "prompt_build", "context_trim"):
                if timing_key in data["data"]:
                    data["data"][timing_key] = 0
        out.append(f'data: {json.dumps(data)}\n\n')
    return out


def _make_fake_get_setting(overrides=None):
    """Fake `get_setting(key, default)` for offline, deterministic runs.

    `agent_syntax_gate` resolves True and `agent_verify_execute` resolves
    False unless explicitly overridden by the caller; every other key
    returns its own supplied default (never touches disk).
    """
    overrides = overrides or {}

    def fake_get_setting(key, default=None):
        if key in overrides:
            return overrides[key]
        if key == "agent_syntax_gate":
            return True
        if key == "agent_verify_execute":
            return False
        return default

    return fake_get_setting


def _make_fake_stream(rounds):
    """rounds: list of chunk-lists, one list per LLM call (= one round each).

    The Nth call to `stream_llm_with_fallback` consumes `rounds[N]`; if the
    loop asks for more rounds than were scripted, the LAST scripted round is
    replayed (keeps tests robust to an extra round without needing to script
    every possible continuation).
    """
    state = {"n": 0}

    async def fake_stream_llm_with_fallback(*_args, **_kwargs):
        idx = min(state["n"], len(rounds) - 1)
        state["n"] += 1
        for chunk in rounds[idx]:
            yield chunk

    return fake_stream_llm_with_fallback


async def _fake_execute_tool_block(block, session_id=None, disabled_tools=None,
                                    owner=None, progress_cb=None):
    """Stub for `execute_tool_block` (Plan 03) -- no existing test in this
    file exercises real tool execution (all prior tests keep tool_blocks
    empty via session_id=None), so this default is a pure addition, never a
    behavior change for existing tests. Needed because Plan 03 turns
    session_id ON (to exercise the auto-create-document fallback) and the
    REAL `execute_tool_block` -> `do_create_document` chain performs actual
    DB persistence -- out of scope for a gate-focused unit/integration test
    and unsafe against the fully-mocked sqlalchemy/core.database stubs."""
    return "tool executed (stub)", {"exit_code": 0, "output": ""}


async def run_chat(monkeypatch, rounds, *, settings=None, message_log=None, **loop_kwargs):
    """Drive the real `stream_agent_loop` end-to-end, offline.

    `rounds` is a list of chunk-lists (one list of SSE-shaped strings per
    scripted LLM round). Returns the ordered list of yielded SSE strings.

    `message_log`, if given a list, is appended with a shallow copy of the
    `messages` argument passed to `stream_llm_with_fallback` on EVERY round
    (Plan 03's UNIFY-02(e) test needs to inspect the combined fix-up system
    message built for the next round's prompt, which is never itself
    streamed as an SSE event -- only the "fixing..." note delta is)."""
    fake_get_setting = _make_fake_get_setting(settings)

    # LOAD-BEARING (06-RESEARCH.md correction to its own Wave-0 note): the
    # monkeypatch target is src.agent_loop.stream_llm_with_fallback -- the
    # name bound INTO the agent_loop namespace at import time
    # (src/agent_loop.py:19, `from src.llm_core import ... stream_llm_with_fallback`)
    # and called at src/agent_loop.py:1775. Patching src.llm_core.stream_llm_with_fallback
    # instead would NOT intercept the loop's call, because agent_loop already
    # holds its own bound reference to the original function object.
    #
    # Patched via the live `agent_loop` MODULE OBJECT (not a
    # "src.agent_loop.stream_llm_with_fallback" string path): a string path
    # would make monkeypatch call importlib.import_module("src.agent_loop"),
    # which -- since this file's own cleanup step never drops "src.agent_loop"
    # from sys.modules (see the NOTE above) -- resolves back to this exact
    # object, but relying on that string form is fragile; the direct object
    # reference is unambiguous and is what actually fixed a real bug found
    # while building this harness (a string-path patch silently re-imported a
    # second, unpatched copy of agent_loop and let a REAL network call reach
    # http://localhost:11434, surfacing as a live "405 method not allowed").
    _base_fake_stream = _make_fake_stream(rounds)

    async def _capturing_fake_stream(*args, **kwargs):
        if message_log is not None:
            # args[1] is `messages` (src/agent_loop.py:1793-1795, positional).
            message_log.append(list(args[1]))
        async for chunk in _base_fake_stream(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", _capturing_fake_stream)
    monkeypatch.setattr(agent_loop, "execute_tool_block", _fake_execute_tool_block)

    # get_setting must be patched at BOTH sites:
    #  - agent_loop.get_setting: the name pre-bound into agent_loop's own
    #    namespace at import time (`from src.settings import get_setting`),
    #    used by agent_loop's ~12 direct call sites (agent_syntax_gate,
    #    agent_stuck_rounds, agent_input_token_budget, agent_episodic_memory, ...).
    #  - settings_module.get_setting (src.settings.get_setting): the source
    #    attribute itself, used by code that does a FRESH
    #    `from src.settings import get_setting` at call time rather than
    #    agent_loop's pre-bound name -- e.g. get_builtin_overrides() (defined
    #    in agent_loop.py, but re-imports locally) and
    #    src.teacher_escalation.run_teacher_inline's inline gate check, both
    #    invoked unconditionally on every turn.
    monkeypatch.setattr(agent_loop, "get_setting", fake_get_setting)
    monkeypatch.setattr(settings_module, "get_setting", fake_get_setting)

    # Offline seams: no live MCP/tool-index/DB stack required.
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(tool_index_module, "get_tool_index", lambda: None)

    default_kwargs = dict(
        endpoint_url="http://localhost:11434",
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        owner="owner-1",
        # Keep the auto-create-document fallback OFF by default (requires a
        # truthy session_id) -- this plan's harness scope is the answer-text
        # syntax gate only; the doc-body seam is Plan 02/03's concern.
        session_id=None,
        max_rounds=len(rounds) + 2,
    )
    default_kwargs.update(loop_kwargs)

    events = []
    async for chunk in agent_loop.stream_agent_loop(**default_kwargs):
        events.append(chunk)
    return events


# ---------------------------------------------------------------------------
# Task 1: smoke test -- the harness can drive a plain-prose turn to completion
# ---------------------------------------------------------------------------

class TestHarnessSmoke:
    async def test_plain_prose_turn_completes_cleanly(self, monkeypatch):
        events = await run_chat(monkeypatch, [_delta_chunks("hi there")])

        assert events, "expected a non-empty SSE stream"
        assert "hi there" in "".join(_extract_deltas(events))
        assert not any(e.startswith("event: error") for e in events)


# ---------------------------------------------------------------------------
# Task 2: PRE-D-02 baseline characterization of the syntax gate
# ---------------------------------------------------------------------------

class TestGateCandidatesHelper:
    """Isolated unit tests for the module-level `_gate_candidates` pure
    helper (Task 1) -- no harness needed, no stream_agent_loop locals."""

    def test_no_trigger_returns_empty(self):
        assert agent_loop._gate_candidates(
            "prose only, no fence", [], answer_reachable=True,
        ) == []

    def test_py_fence_trigger_returns_one_answer_candidate(self):
        text = "```py\ndef broken(:\n    pass\n```"
        assert agent_loop._gate_candidates(
            text, [], answer_reachable=True,
        ) == [("answer block", text)]

    def test_answer_not_reachable_returns_empty(self):
        # A round that fired a real (non-document) tool must not gate its
        # prose -- answer_reachable=False mirrors that today's
        # `if not tool_blocks:` skipped it.
        text = "```py\ndef broken(:\n    pass\n```"
        real_tool_block = agent_loop.ToolBlock("read_file", "some content")
        assert agent_loop._gate_candidates(
            text, [real_tool_block], answer_reachable=False,
        ) == []


class TestGateCandidatesDocumentSurface:
    """Plan 03 (UNIFY-02 d): `_gate_candidates` extended to the
    `create_document` Python document surface -- isolated unit tests, no
    harness needed."""

    def test_python_create_document_returns_fence_wrapped_candidate_only(self):
        # answer_text has NO "```py" fence -- the answer surface must NOT
        # produce a candidate; only the document surface should.
        python_doc = agent_loop.ToolBlock(
            "create_document", "My Doc\npython\nprint('hi')",
        )
        cands = agent_loop._gate_candidates(
            "prose only, no fence here", [python_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        label, text = cands[0]
        assert label == 'document "My Doc"'
        assert text.startswith("```python\n")
        assert text.endswith("```")
        assert "print('hi')" in text

    def test_py_lang_alias_also_gated(self):
        # The lightweight split's lang line uses whatever the doc actually
        # stored -- "py" must be treated as Python too (matches D-03's
        # Python-only scope, not a literal "python" string match).
        py_doc = agent_loop.ToolBlock("create_document", "Snippet\npy\nx = 1")
        cands = agent_loop._gate_candidates("", [py_doc], answer_reachable=True)
        assert len(cands) == 1
        assert cands[0][0] == 'document "Snippet"'
        assert cands[0][1] == "```python\nx = 1\n```"

    def test_non_python_document_produces_no_candidate(self):
        html_doc = agent_loop.ToolBlock(
            "create_document", "My Doc\nhtml\n<div>hi</div>",
        )
        assert agent_loop._gate_candidates(
            "", [html_doc], answer_reachable=True,
        ) == []

    def test_gate_candidates_does_not_mutate_block_content(self):
        original_content = "My Doc\npython\nprint('hi')"
        block = agent_loop.ToolBlock("create_document", original_content)
        agent_loop._gate_candidates("", [block], answer_reachable=True)
        assert block.content == original_content

    def test_multiple_create_document_blocks_all_considered(self):
        py_doc = agent_loop.ToolBlock("create_document", "Py Doc\npython\nprint(1)")
        js_doc = agent_loop.ToolBlock("create_document", "JS Doc\njavascript\nvar x=1;")
        cands = agent_loop._gate_candidates(
            "", [py_doc, js_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        assert cands[0][0] == 'document "Py Doc"'

    def test_document_surface_not_reachable_returns_empty(self):
        # CR-01 regression guard: a round that is NOT answer-reachable (i.e.
        # a real, non-document tool is also pending in `tool_blocks`) must
        # produce ZERO candidates on EITHER surface -- including the
        # document surface. Before the fix, this loop ignored
        # `answer_reachable` entirely and still emitted a document
        # candidate here, which let the gate fire and `continue` before the
        # real pending tool ever executed.
        broken_py_doc = agent_loop.ToolBlock(
            "create_document", "My Doc\npython\ndef broken(:\n    pass",
        )
        assert agent_loop._gate_candidates(
            "some answer text", [broken_py_doc], answer_reachable=False,
        ) == []


class TestGateCandidatesWidenedDocWriteScope:
    """SEC-11 (Plan 06, gap-closed by Plan 09/CR-01): `_gate_candidates`
    widened past create_document-only scope to EVERY doc-write tool type
    (update_document/edit_document/suggest_document) x every fence language
    the gate can verify (python/bash/sql), reusing `verified_gate.evaluate`
    verbatim. Replaces the old `test_update_document_not_gated` test
    (Plan 03), which pinned the NOW-STALE create_document-only scope
    boundary this plan removes.

    GAP-CLOSURE (Plan 09, CR-01): every update_document/edit_document/
    suggest_document fixture below is built in tool_schemas.py's REAL
    runtime content shape (src/tool_schemas.py:1214-1229) -- a raw body for
    update_document, `<<<FIND>>>/<<<REPLACE>>>/<<<END>>>` markers for
    edit_document, `<<<FIND>>>/<<<SUGGEST>>>/<<<REASON>>>/<<<END>>>` markers
    for suggest_document -- NEVER create_document's `title\n[lang]\ncontent`
    3-line-header shape (tool_schemas.py never produces that shape for these
    three tools). The Plan 06 fixtures this class replaced used the
    create_document shape for all four tool types and therefore validated a
    fictional content shape (masking tests) -- see 20-VERIFICATION.md. A
    future regression back to create_document-shaped bodies for these three
    tool types should make the discriminating tests below fail again."""

    def test_update_document_gated(self):
        # update_document's REAL runtime shape (tool_schemas.py:1229) is a
        # RAW body -- no title, no language header. The document's language
        # is not in the ToolBlock (D-GAP-01); the ONLY derivable signal is
        # an embedded fenced code block the model itself wrote.
        update_doc = agent_loop.ToolBlock(
            "update_document", "```python\nprint('hi')\n```",
        )
        cands = agent_loop._gate_candidates(
            "", [update_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        label, text = cands[0]
        assert label == "update_document"
        assert text == "```python\nprint('hi')\n```"

    def test_edit_document_gated(self):
        # edit_document's REAL runtime shape (tool_schemas.py:1214-1220) is
        # <<<FIND>>>/<<<REPLACE>>>/<<<END>>> markers -- gate the REPLACE
        # body (via parse_edit_blocks) only when IT carries a fence.
        edit_doc = agent_loop.ToolBlock(
            "edit_document",
            "<<<FIND>>>\nx = 1\n<<<REPLACE>>>\n```python\nx = 2\n```\n<<<END>>>",
        )
        cands = agent_loop._gate_candidates(
            "", [edit_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        assert cands[0][0] == "edit_document"
        assert cands[0][1] == "```python\nx = 2\n```"

    def test_suggest_document_gated(self):
        # suggest_document's REAL runtime shape (tool_schemas.py:1221-1227)
        # is <<<FIND>>>/<<<SUGGEST>>>/<<<REASON>>>/<<<END>>> markers -- gate
        # the SUGGEST body (via parse_suggest_blocks) only when IT carries a
        # fence. Reason text avoids parse_suggest_blocks's own skip-phrase
        # list ("clear" among them) so this suggestion is not discarded as
        # a no-op before it ever reaches `_gate_candidates`.
        suggest_doc = agent_loop.ToolBlock(
            "suggest_document",
            "<<<FIND>>>\nx = 1\n<<<SUGGEST>>>\n```python\nx = 2\n```\n"
            "<<<REASON>>>\nCorrects a syntax issue\n<<<END>>>",
        )
        cands = agent_loop._gate_candidates(
            "", [suggest_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        assert cands[0][0] == "suggest_document"
        assert cands[0][1] == "```python\nx = 2\n```"

    def test_update_document_syntax_error_flips_needs_repair(self):
        # A genuine syntax-error Python body embedded in an update_document
        # RAW body must actually flip needs_repair via verified_gate.evaluate
        # -- not just be produced as a candidate. Proves the gate genuinely
        # RUNS on update/edit bodies now, not merely that a candidate is
        # emitted.
        broken_doc = agent_loop.ToolBlock(
            "update_document", "```python\ndef broken(:\n    pass\n```",
        )
        cands = agent_loop._gate_candidates(
            "", [broken_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        _, text = cands[0]
        verdict, needs_repair, why = verified_gate.evaluate(text)
        assert needs_repair is True
        assert why == "did not parse"

    def test_update_document_gate_spy_fires_on_real_call_site_binding(self, monkeypatch):
        # Spy on the CALLING module's bound name (agent_loop.py does
        # `from src import verified_gate as _vg` -- name-binding trap per
        # 20-PATTERNS.md), proving a real call site invokes the gate for an
        # update_document candidate through the SAME bound reference the
        # production code path uses -- not `src.verified_gate.evaluate`
        # patched at the source module (which would not be observed here).
        calls = []
        real_evaluate = agent_loop._vg.evaluate

        def spy_evaluate(candidate, **kwargs):
            calls.append(candidate)
            return real_evaluate(candidate, **kwargs)

        monkeypatch.setattr(agent_loop._vg, "evaluate", spy_evaluate)
        update_doc = agent_loop.ToolBlock(
            "update_document", "```python\nprint('hi')\n```",
        )
        cands = agent_loop._gate_candidates(
            "", [update_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        _, text = cands[0]
        agent_loop._vg.evaluate(text)
        assert calls == [text]

    def test_update_document_bash_fence_gated(self):
        bash_doc = agent_loop.ToolBlock(
            "update_document", "```bash\necho hi\n```",
        )
        cands = agent_loop._gate_candidates(
            "", [bash_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        assert cands[0][1] == "```bash\necho hi\n```"

    def test_update_document_sql_fence_gated(self):
        sql_doc = agent_loop.ToolBlock(
            "update_document", "```sql\nSELECT 1;\n```",
        )
        cands = agent_loop._gate_candidates(
            "", [sql_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        assert cands[0][1] == "```sql\nSELECT 1;\n```"

    def test_update_document_prose_noop(self):
        # update_document's real shape is a raw body -- unfenced plain
        # prose carries no derivable language and must honestly skip (no
        # candidate) -- matching verify.py's unverified_languages semantics.
        prose_doc = agent_loop.ToolBlock(
            "update_document",
            "This is a plain description with no code fence at all.",
        )
        assert agent_loop._gate_candidates(
            "", [prose_doc], answer_reachable=True,
        ) == []

    def test_update_document_unfenced_source_honest_skip(self):
        # HONEST-SKIP case, matching 20-VERIFICATION.md's on-machine repro
        # shape exactly: raw Python source with NO embedded fence. The
        # document's language lives only in the DB (D-GAP-01) -- not
        # derivable from the ToolBlock -- so this MUST honestly skip, never
        # be misparsed into a false candidate.
        unfenced_doc = agent_loop.ToolBlock(
            "update_document", "def broken(:\n    pass\n",
        )
        assert agent_loop._gate_candidates(
            "", [unfenced_doc], answer_reachable=True,
        ) == []

    def test_edit_document_unfenced_replace_honest_skip(self):
        # HONEST-SKIP case for edit_document, matching 20-VERIFICATION.md's
        # on-machine repro shape exactly: real FIND/REPLACE/END markers
        # whose REPLACE body carries no embedded fence.
        edit_doc = agent_loop.ToolBlock(
            "edit_document",
            "<<<FIND>>>\ndef ok():\n    pass\n<<<REPLACE>>>\n"
            "def broken(:\n    pass\n<<<END>>>",
        )
        assert agent_loop._gate_candidates(
            "", [edit_doc], answer_reachable=True,
        ) == []

    def test_html_update_document_still_not_gated(self):
        # A fence language the gate has no validator for (e.g. html) stays
        # ungated too -- widening the tool-type set does not widen the
        # language set past what verified_gate can honestly verify.
        html_doc = agent_loop.ToolBlock(
            "update_document", "```html\n<div>hi</div>\n```",
        )
        assert agent_loop._gate_candidates(
            "", [html_doc], answer_reachable=True,
        ) == []

    def test_create_document_unchanged_shape_still_gated(self):
        # create_document's REAL shape (tool_schemas.py:1208-1213) IS the
        # title\n[language]\ncontent positional layout -- unchanged by this
        # gap-closure. Proves the create path survives the per-tool_type
        # rewrite untouched.
        create_doc = agent_loop.ToolBlock(
            "create_document", "Doc Title\npython\nprint('hi')",
        )
        cands = agent_loop._gate_candidates(
            "", [create_doc], answer_reachable=True,
        )
        assert len(cands) == 1
        label, text = cands[0]
        assert label == 'document "Doc Title"'
        assert text == "```python\nprint('hi')\n```"

    def test_alias_names_normalize_to_gated_canonical_types(self):
        # Aliases are normalized upstream in tool_parsing.py::_TOOL_NAME_MAP
        # BEFORE `_gate_candidates` ever sees the block -- no alias handling
        # is (or should be) added inside `_gate_candidates` itself (T-20-06-B).
        assert tool_parsing._TOOL_NAME_MAP["edit"] == "edit_document"
        assert tool_parsing._TOOL_NAME_MAP["document"] == "update_document"
        assert tool_parsing._TOOL_NAME_MAP["edit"] in agent_loop._DOC_WRITE_TOOL_TYPES
        assert tool_parsing._TOOL_NAME_MAP["document"] in agent_loop._DOC_WRITE_TOOL_TYPES


class TestCallSiteWidenedForRealDocWriteToolCalls:
    """Gap-closure (Plan 09) companion to `TestCreateDocumentToolCallGated`
    (T-6-02/WR-01). That class proved the entry-trigger widening for
    create_document; THIS class proves the SAME call-site checks
    (`_answer_reachable`, and the entry trigger renamed from
    `_has_create_doc` to `_has_doc_write_tool`) now also cover a genuine
    (non-auto-created) update_document tool call end-to-end -- not merely a
    direct `_gate_candidates` unit call with `answer_reachable` hardcoded
    True (every test in `TestGateCandidatesWidenedDocWriteScope` and
    20-VERIFICATION.md's own repro use that shortcut).

    Rule 1 deviation (discovered during Plan 09 execution, not itself
    CR-01): Plan 06/SEC-11 widened `_gate_candidates`'s internal tool-type
    filter (`_DOC_WRITE_TOOL_TYPES`) to all four doc-write types, but left
    BOTH call-site checks (`_answer_reachable`'s `all(...)` and the entry
    trigger) narrowed to `== "create_document"` only. A round whose ONLY
    tool call was update_document/edit_document/suggest_document (never
    create_document) therefore left `_answer_reachable` False and the entry
    trigger unmet, so `_gate_candidates` was never even INVOKED for this
    real traffic shape -- only the auto-create-document fallback (which
    always builds a create_document-typed ToolBlock) ever reached the
    CR-01 parsing fix. Left unfixed, this Plan's own CR-01 fix would have
    been inert for genuine update/edit/suggest_document calls in
    production -- the same false-assurance failure mode this gap-closure
    exists to eliminate, one level up from the parsing bug. See
    20-09-SUMMARY.md for the full writeup.

    Built via the REAL `function_call_to_tool_block` native-tool-calling
    conversion path (`_tool_call_chunks`), never a fenced ```update_document
    block -- a fenced representation cannot carry a body with an embedded
    ``` fence without an ambiguous nested-fence parse."""

    async def test_broken_update_document_native_call_fires_gate(self, monkeypatch):
        broken_body = "```python\ndef broken(:\n    pass\n```"
        fixed_answer = "Fixed the document, here's a clean version."
        events = await run_chat(
            monkeypatch,
            [
                _tool_call_chunks("update_document", {"content": broken_body, "doc_id": "doc-1"}),
                _delta_chunks(fixed_answer),
            ],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" in deltas
        assert fixed_answer in deltas
        # Exactly one repair round fired: round 1 -> round 2, nothing beyond.
        assert _agent_step_rounds(events) == [2]
        # The broken update must NEVER have been executed -- the gate's
        # `continue` fires before tool execution is reached.
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "update_document"]
        assert doc_starts == []

    async def test_valid_update_document_native_call_no_gate_fire(self, monkeypatch):
        valid_body = "```python\nprint('hello')\n```"
        round2_response = "Document updated successfully."
        events = await run_chat(
            monkeypatch,
            [
                _tool_call_chunks("update_document", {"content": valid_body, "doc_id": "doc-1"}),
                _delta_chunks(round2_response),
            ],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" not in deltas
        assert _agent_step_rounds(events) == [2]
        assert round2_response in deltas
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "update_document"]
        assert len(doc_starts) == 1

    async def test_unfenced_update_document_native_call_honest_skip_no_gate_fire(self, monkeypatch):
        # Matches 20-VERIFICATION.md's on-machine repro exactly: raw
        # unfenced Python source (the open document's own language, not
        # derivable from the ToolBlock, D-GAP-01) -- honest skip, no
        # repair round, tool executes normally through to completion.
        unfenced_body = "def broken(:\n    pass\n"
        round2_response = "Document updated."
        events = await run_chat(
            monkeypatch,
            [
                _tool_call_chunks("update_document", {"content": unfenced_body, "doc_id": "doc-1"}),
                _delta_chunks(round2_response),
            ],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" not in deltas
        assert _agent_step_rounds(events) == [2]
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "update_document"]
        assert len(doc_starts) == 1


class TestBuildFixupMessageHelper:
    """Isolated unit tests for the module-level `_build_fixup_message` pure
    helper (Task 1) -- must aggregate 1..N failures into ONE message."""

    def test_single_failure_names_surface_and_reason(self):
        msg = agent_loop._build_fixup_message(
            [("answer block", "did not parse", "line 2: invalid syntax")]
        )
        assert "answer block" in msg
        assert "did not parse" in msg
        assert "line 2: invalid syntax" in msg
        assert "Rewrite the corrected final answer now" in msg
        assert "Do not call any tools" in msg

    def test_two_failures_names_both_surfaces(self):
        msg = agent_loop._build_fixup_message(
            [
                ("answer block", "did not parse", "detail1"),
                ("document <Foo>", "failed when run", "detail2"),
            ]
        )
        assert "answer block" in msg
        assert "document <Foo>" in msg
        assert "did not parse" in msg
        assert "failed when run" in msg


class TestPreD02SyntaxGateBaseline:
    """Locks in the CURRENT (unmodified) `src/agent_loop.py` gate behavior,
    run BEFORE Plan 02's D-02 swap touches production code."""

    async def test_gate_fires_on_broken_py_block(self, monkeypatch):
        # Case A: a final answer whose only content is a ```py fenced block
        # (the alias, NOT ```python -- Pitfall 3) of 3+ lines with a genuine
        # Python syntax error (unbalanced paren, missing colon).
        broken_answer = (
            "```py\n"
            "def broken(\n"
            "    x = 1\n"
            "    return x\n"
            "```"
        )
        events = await run_chat(
            monkeypatch,
            [
                _delta_chunks(broken_answer),
                _delta_chunks("Fixed the code, here is a working version."),
            ],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" in deltas
        # Exactly one repair round fired: round 1 -> round 2, and nothing beyond.
        assert _agent_step_rounds(events) == [2]

    async def test_gate_silent_on_untagged_non_python_fence(self, monkeypatch):
        # Case B (Pitfall-2 non-regression anchor): a final answer whose only
        # fenced content is an UNTAGGED fence with non-Python content (a YAML
        # snippet). The current gate's outer trigger guard is a literal
        # "```py" substring check, which this fence does NOT contain, so
        # TODAY's gate must stay silent -- zero repair rounds, zero fixing
        # note. This is the literal counter-example D-02's regex-widening
        # (mandatory tag -> optional tag) must not break in Plan 02.
        yaml_answer = (
            "Here is the config:\n\n"
            "```\n"
            "some_key: some_value\n"
            "list:\n"
            "  - a\n"
            "  - b\n"
            "```\n"
        )
        events = await run_chat(monkeypatch, [_delta_chunks(yaml_answer)])

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" not in deltas
        assert _agent_step_rounds(events) == []
        assert not any(e.startswith("event: error") for e in events)


# ---------------------------------------------------------------------------
# Task 2: post-swap behavior tests (a), (b), (c), (f) -- Chat's answer-text
# surface now routes through verified_gate.evaluate/resolve (D-02), not the
# removed bespoke `_python_syntax_errors` checker.
# ---------------------------------------------------------------------------

class TestSharedGateBehavior:
    async def test_a_broken_py_block_drives_one_repair_round(self, monkeypatch):
        # UNIFY-02 (a): a no-tool final answer with a broken ```py block
        # drives exactly one repair round and the scripted corrected answer
        # is returned in the final stream.
        broken_answer = (
            "```py\n"
            "def broken(\n"
            "    x = 1\n"
            "    return x\n"
            "```"
        )
        fixed_answer = "Fixed the code, here is a working version."
        events = await run_chat(
            monkeypatch,
            [_delta_chunks(broken_answer), _delta_chunks(fixed_answer)],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" in deltas
        assert fixed_answer in deltas
        # Exactly one repair round fired: round 1 -> round 2, nothing beyond.
        assert _agent_step_rounds(events) == [2]

    async def test_b_spy_proves_shared_core_invoked(self, monkeypatch):
        # UNIFY-02 (b): two-layer spy (mirrors
        # tests/test_answer_pipeline_guard.py:241-287) -- stub the deep
        # dependency `src.council.verify.verify_answer` so no real subprocess
        # runs, then spy on `verified_gate.evaluate` itself to prove Chat
        # genuinely INVOKES the shared core (not a bespoke copy).
        broken_answer = "```py\ndef broken(:\n    pass\n```"

        call_state = {"n": 0}

        def fake_verify_answer(text, execute=False):
            call_state["n"] += 1
            if call_state["n"] == 1:
                return {"passed": False, "has_code": True, "syntax_ok": False,
                        "detail": "SyntaxError: invalid syntax"}
            return {"passed": True, "has_code": True, "syntax_ok": True, "detail": ""}

        monkeypatch.setattr("src.council.verify.verify_answer", fake_verify_answer)

        spy_calls = {"n": 0}
        real_evaluate = verified_gate.evaluate

        def spy_evaluate(candidate, *, execute=False):
            spy_calls["n"] += 1
            return real_evaluate(candidate, execute=execute)

        monkeypatch.setattr(verified_gate, "evaluate", spy_evaluate)

        events = await run_chat(
            monkeypatch,
            [_delta_chunks(broken_answer), _delta_chunks("Fixed version here.")],
        )

        assert spy_calls["n"] >= 1
        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" in deltas

    async def test_c_non_code_turns_byte_identical_to_gate_disabled(self, monkeypatch):
        # UNIFY-02 (c): a plain-prose turn AND an untagged non-Python-fence
        # turn each produce ZERO fixing notes/rounds -- byte-identical to a
        # run with agent_syntax_gate=False (structural no-op, D-04).
        prose = "Just a plain prose answer, nothing fenced here."
        events_on = await run_chat(monkeypatch, [_delta_chunks(prose)])
        events_off = await run_chat(
            monkeypatch, [_delta_chunks(prose)],
            settings={"agent_syntax_gate": False},
        )
        assert _normalize_events(events_on) == _normalize_events(events_off)

        yaml_answer = (
            "Here is the config:\n\n"
            "```\n"
            "some_key: some_value\n"
            "```\n"
        )
        events_on2 = await run_chat(monkeypatch, [_delta_chunks(yaml_answer)])
        events_off2 = await run_chat(
            monkeypatch, [_delta_chunks(yaml_answer)],
            settings={"agent_syntax_gate": False},
        )
        assert _normalize_events(events_on2) == _normalize_events(events_off2)

    async def test_f_raising_verifier_does_not_crash_turn(self, monkeypatch):
        # UNIFY-02 (f): with `verify_answer` monkeypatched to raise, the turn
        # still completes and no exception propagates into the SSE stream
        # (never-raises holds through the Chat call site, T-6-03).
        broken_answer = "```py\ndef broken(:\n    pass\n```"

        def raising_verify_answer(text, execute=False):
            raise RuntimeError("boom")

        monkeypatch.setattr("src.council.verify.verify_answer", raising_verify_answer)

        events = await run_chat(monkeypatch, [_delta_chunks(broken_answer)])

        assert events, "expected a non-empty SSE stream"
        assert not any(e.startswith("event: error") for e in events)
        # No repair round fired -- the raising verifier means the candidate
        # was treated as unverifiable, not as a verified failure.
        assert _agent_step_rounds(events) == []


# ---------------------------------------------------------------------------
# Plan 03, Task 1: UNIFY-02 (d) -- document-body Python code is verified via
# the extended `_gate_candidates`, even when the auto-create fallback has
# already made `tool_blocks` non-empty (06-RESEARCH.md Pitfall 1's exact
# counter-example).
# ---------------------------------------------------------------------------

class TestDocumentBodyGateEndToEnd:
    async def test_d_auto_created_broken_doc_fires_repair_round(self, monkeypatch):
        # A 40-line ```py block (well past the 30-line auto-create threshold,
        # src/agent_loop.py:2004) with a genuine syntax error (unbalanced
        # paren). The auto-create fallback moves it into a `create_document`
        # ToolBlock BEFORE the gate section runs, making `tool_blocks`
        # non-empty for this round.
        big_broken_lines = ["def broken_big(("] + [f"    x{i} = {i}" for i in range(40)]
        big_broken_body = "\n".join(big_broken_lines)
        big_broken = f"```py\n{big_broken_body}\n```"
        fixed_answer = "Fixed the document, here's a clean version."

        events = await run_chat(
            monkeypatch,
            [_delta_chunks(big_broken), _delta_chunks(fixed_answer)],
            session_id="sess-1",
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" in deltas
        assert fixed_answer in deltas
        # Exactly one repair round fired -- despite tool_blocks being
        # non-empty (the auto-created document), the gate still fired.
        assert _agent_step_rounds(events) == [2]

    async def test_non_python_auto_created_doc_is_a_natural_no_op(self, monkeypatch):
        # A small, VALID inline ```py snippet keeps the outer "```py" trigger
        # guard satisfied so the gate section actually runs (and gets a
        # chance to emit a document candidate); a separate 40-line ```js
        # block auto-creates a NON-Python document. The document candidate
        # must be skipped (D-03 natural no-op) -- no repair round. Since the
        # round DID perform a real tool action (the auto-created doc),
        # tool_blocks is non-empty and the turn naturally continues to a
        # second round (an ordinary agent_step, not a gate/loop-breaker
        # artifact) -- round 2 is a plain tool-free answer that finishes it.
        js_lines = ["function brokenJs() {"] + [f"  var x{i} = {i};" for i in range(40)]
        js_body = "\n".join(js_lines)
        round1_response = "```py\nprint('ok')\n```\n\n" + f"```js\n{js_body}\n```"
        round2_response = "All good, document created."

        events = await run_chat(
            monkeypatch,
            [_delta_chunks(round1_response), _delta_chunks(round2_response)],
            session_id="sess-1",
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" not in deltas
        # The single agent_step is the ordinary continuation after a real
        # tool action -- NOT a gate repair round.
        assert _agent_step_rounds(events) == [2]
        assert round2_response in deltas


# ---------------------------------------------------------------------------
# Plan 03, Task 2: UNIFY-02 (e) -- one combined fix-up round aggregates a
# broken answer-text block AND a broken auto-created document body, naming
# BOTH surfaces; the loop-breaker machinery never sees the gate round.
# ---------------------------------------------------------------------------

class TestCombinedAggregation:
    async def test_e_combined_round_names_both_surfaces_single_use(self, monkeypatch):
        small_broken = "```py\ndef broken(:\n    pass\n```"
        big_broken_lines = ["def broken_big(("] + [f"    x{i} = {i}" for i in range(40)]
        big_broken_body = "\n".join(big_broken_lines)
        big_broken = f"```py\n{big_broken_body}\n```"
        round1_response = small_broken + "\n\n" + big_broken

        # Round 2's response is ALSO broken (small block again) -- proves
        # `_syntax_gate_used` is single-use per turn: a second broken round
        # must NOT fire a second gate round.
        round2_response = "```py\ndef still_broken(:\n    pass\n```"

        message_log = []
        events = await run_chat(
            monkeypatch,
            [_delta_chunks(round1_response), _delta_chunks(round2_response)],
            session_id="sess-1",
            message_log=message_log,
        )

        deltas = "".join(_extract_deltas(events))
        # Exactly ONE "fixing" note fired, despite round 2 also being broken.
        assert deltas.count("Caught a Python syntax error") == 1
        # Exactly ONE repair round scheduled (round1 -> round2); the turn
        # then completes (round 2's gate is skipped -- single-use -- and its
        # empty tool_blocks take the normal no-tools completion path).
        assert _agent_step_rounds(events) == [2]

        # The combined fix-up system message (sent as part of round 2's
        # prompt, never itself streamed as an SSE delta) must name BOTH
        # surfaces -- the answer block AND the auto-created document title.
        assert len(message_log) == 2  # one LLM call per round
        round2_messages = message_log[1]
        system_contents = [
            m["content"] for m in round2_messages if m.get("role") == "system"
        ]
        combined = "\n".join(system_contents)
        assert "answer block" in combined
        assert 'document "Code (python)"' in combined

        # Loop-breaker invisibility (D-06): the gate round's `continue`
        # happens before src/agent_loop.py's loop-breaker section ever
        # computes `_sig`/`_stuck_rounds`/`_tool_type_counts` for that round,
        # and round 2 (empty tool_blocks, gate skipped -- single-use) takes
        # the immediate no-tools completion `break` -- so the loop-breaker's
        # forced-answer system message never appears anywhere in this turn.
        assert "repeating tool calls without converging" not in combined


# ---------------------------------------------------------------------------
# CR-01 regression guard: a round mixing a REAL (non-document) tool call
# with a broken auto-created document must NOT gate -- the gate's `continue`
# sits BEFORE tool execution (src/agent_loop.py ~2235), so gating a round
# that still has a pending real tool silently drops that tool call. Before
# the fix, `_gate_candidates`' document-surface loop ignored
# `answer_reachable` and emitted a broken-document candidate even when a
# real tool (e.g. bash) was also pending -- firing the gate and `continue`-
# ing past the bash tool's execution. This class end-to-end drives that
# exact mixed round and asserts the bash tool actually runs.
# ---------------------------------------------------------------------------

class TestCR01MixedRealToolAndBrokenAutoDocRegression:
    async def test_real_tool_survives_alongside_broken_autodoc(self, monkeypatch):
        # Round 1: a genuine ```bash tool call PLUS a >=31-line ```py block
        # with a real syntax error (unbalanced paren). "py" (not "python")
        # is not a TOOL_TAGS member, so it is NOT parsed as a tool call --
        # it stays in `cleaned_round` and, being >=30 lines, gets swept into
        # the auto-create-document fallback (src/agent_loop.py ~2021-2041),
        # making `tool_blocks = [bash_block, auto_doc_block]`.
        big_broken_lines = ["def broken_big(("] + [f"    x{i} = {i}" for i in range(40)]
        big_broken_body = "\n".join(big_broken_lines)
        big_broken = f"```py\n{big_broken_body}\n```"
        round1_response = "```bash\necho hi\n```\n\n" + big_broken
        round2_response = "All done, nothing more to do."

        events = await run_chat(
            monkeypatch,
            [_delta_chunks(round1_response), _delta_chunks(round2_response)],
            session_id="sess-1",
        )

        deltas = "".join(_extract_deltas(events))
        # The gate must NOT fire: no "fixing" note anywhere in the stream.
        assert "Caught a Python syntax error" not in deltas

        # The turn must NOT take an extra gate-repair round -- round 1 flows
        # straight into round 2's ordinary continuation (the real tool
        # executed, so the turn keeps going), not a gate-forced re-ask of
        # the same round.
        assert _agent_step_rounds(events) == [2]
        assert round2_response in deltas

        # The real bash tool must have actually been executed -- not
        # silently dropped by a gate `continue` that fires before tool
        # execution is reached. `tool_start`/`tool_output` are only ever
        # emitted from the tool-execution loop (src/agent_loop.py ~2235+),
        # which sits AFTER the gate's `continue` -- so their presence here
        # is direct proof the mixed round's gate did not eat the bash call
        # (equivalently: the harness's `execute_tool_block` stub, which
        # `run_chat` wires in, was reached and invoked for the bash block).
        bash_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "bash"]
        assert len(bash_starts) == 1
        bash_outputs = [e for e in _typed_events(events, "tool_output") if e["tool"] == "bash"]
        assert len(bash_outputs) == 1
        # exit_code 0 comes from the stubbed `execute_tool_block`'s return
        # value ({"exit_code": 0, "output": ""}) -- confirming the stub was
        # actually invoked and its result flowed through to the SSE stream,
        # not a synthetic/skipped tool.
        assert bash_outputs[0]["exit_code"] == 0

        # The auto-created (broken) document was also executed via the same
        # stub -- unverified, which is the accepted trade-off for the mixed
        # case (CR-01's fix, not a new regression: only the pure auto-doc
        # case, with no other pending tool, keeps document verification).
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "create_document"]
        assert len(doc_starts) == 1


# ---------------------------------------------------------------------------
# T-6-02 / WR-01 closure: the gate-entry trigger widening. A GENUINE
# ```create_document TOOL-CALL block (src/agent_tools.py TOOL_TAGS) is
# stripped out of `cleaned_round` by `strip_tool_blocks` before the old
# trigger's bare `"```py" in cleaned_round` check ever runs, so an explicit
# create_document call's Python body flowed to `do_create_document`
# UNVERIFIED -- only auto-created documents (loose >=30-line ```py blocks,
# which leave the ```py fence in cleaned_round) were ever gated. These tests
# drive the REAL create_document TOOL-CALL path (parsed by
# `parse_tool_blocks` via the `create_document` fence tag, per
# src/tool_parsing.py's `_TOOL_BLOCK_RE`) -- NOT the auto-create fallback the
# tests above use.
# ---------------------------------------------------------------------------

class TestCreateDocumentToolCallGated:
    async def test_broken_create_document_tool_call_fires_gate(self, monkeypatch):
        # T-6-02 / WR-01 closure guard: this is the exact gap the security
        # auditor and code reviewer flagged as OPEN/HIGH -- a round whose
        # ONLY Python code is inside a genuine ```create_document tool call
        # (not a loose ```py block) must still be gated. Against the
        # PRE-FIX trigger (bare "```py" in cleaned_round), this test FAILS:
        # cleaned_round is EMPTY (the whole tool block was stripped), so the
        # gate is never entered, no repair round fires, and the broken body
        # would flow straight to `do_create_document` unverified. Verified
        # manually against a temporary revert of the trigger widening.
        broken_call = (
            "```create_document\n"
            "Broken Doc\n"
            "python\n"
            "def broken(:\n"
            "    pass\n"
            "```"
        )
        fixed_answer = "Fixed the document, here's a clean version."
        events = await run_chat(
            monkeypatch,
            [_delta_chunks(broken_call), _delta_chunks(fixed_answer)],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" in deltas
        assert fixed_answer in deltas
        # Exactly one repair round fired: round 1 -> round 2, nothing beyond.
        assert _agent_step_rounds(events) == [2]
        # The broken document must NEVER have been executed/persisted --
        # the gate's `continue` fires before tool execution is reached.
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "create_document"]
        assert doc_starts == []

    async def test_valid_create_document_tool_call_no_gate_fire(self, monkeypatch):
        # A genuine create_document call with a VALID Python body must NOT
        # trigger a repair round -- the widened trigger enters the gate
        # section, but `_gate_candidates`/`_vg.evaluate` find nothing to
        # repair, so the document flows through to normal tool execution.
        valid_call = (
            "```create_document\n"
            "Valid Doc\n"
            "python\n"
            "print('hello')\n"
            "```"
        )
        round2_response = "Document created successfully."
        events = await run_chat(
            monkeypatch,
            [_delta_chunks(valid_call), _delta_chunks(round2_response)],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" not in deltas
        # Ordinary continuation after the document tool executes -- NOT a
        # gate repair round.
        assert _agent_step_rounds(events) == [2]
        assert round2_response in deltas
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "create_document"]
        assert len(doc_starts) == 1

    async def test_non_python_create_document_tool_call_natural_no_op(self, monkeypatch):
        # A genuine create_document call with a NON-Python body (html) must
        # produce NO candidate on the document surface (D-03 scope is
        # strictly Python) -- the widened trigger still enters the gate
        # section (because a create_document block is present), but
        # `_gate_candidates` skips the non-Python body, so no repair round
        # fires. Natural no-op, not a special case in the trigger itself.
        html_call = (
            "```create_document\n"
            "My Page\n"
            "html\n"
            "<div>hi</div>\n"
            "```"
        )
        round2_response = "Document created."
        events = await run_chat(
            monkeypatch,
            [_delta_chunks(html_call), _delta_chunks(round2_response)],
        )

        deltas = "".join(_extract_deltas(events))
        assert "Caught a Python syntax error" not in deltas
        assert _agent_step_rounds(events) == [2]
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "create_document"]
        assert len(doc_starts) == 1

    async def test_mixed_round_with_real_tool_not_gated_cr01_residual(self, monkeypatch):
        # Intentional mixed-round residual (see commit body / 06-REVIEW.md):
        # a round pairing a REAL non-document tool (bash) with a genuine
        # ```create_document broken-Python tool call is NOT gated --
        # `answer_reachable=False` (CR-01's guard) makes `_gate_candidates`
        # early-return [] regardless of the widened trigger, so the pending
        # bash tool is never dropped. This is the correct tradeoff (drop a
        # verification chance on the document body vs. silently dropping
        # the user's real tool call) and is inherent to the one-round-per-
        # turn + `continue` mechanism -- not a new gap.
        broken_call = (
            "```bash\necho hi\n```\n\n"
            "```create_document\n"
            "Broken Doc\n"
            "python\n"
            "def broken(:\n"
            "    pass\n"
            "```"
        )
        round2_response = "All done, nothing more to do."
        events = await run_chat(
            monkeypatch,
            [_delta_chunks(broken_call), _delta_chunks(round2_response)],
        )

        deltas = "".join(_extract_deltas(events))
        # The gate must NOT fire -- the real bash tool is not dropped.
        assert "Caught a Python syntax error" not in deltas
        assert _agent_step_rounds(events) == [2]
        assert round2_response in deltas

        bash_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "bash"]
        assert len(bash_starts) == 1
        bash_outputs = [e for e in _typed_events(events, "tool_output") if e["tool"] == "bash"]
        assert len(bash_outputs) == 1
        assert bash_outputs[0]["exit_code"] == 0

        # The broken create_document body was also executed via the same
        # stub -- unverified. This is the accepted, narrow residual: only
        # a PURE create_document round (no competing real tool) is
        # guaranteed verification.
        doc_starts = [e for e in _typed_events(events, "tool_start") if e["tool"] == "create_document"]
        assert len(doc_starts) == 1
