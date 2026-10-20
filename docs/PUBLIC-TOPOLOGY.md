# Prisma — Public architecture and execution topology

> **PUBLIC RELEASE / SANITIZED TECHNICAL MAP.** This documents verified source-level components, not private infrastructure. Private brain corpora, user memories, customer-specific specialist configurations, secrets, model weights, machine identifiers and internal planning are excluded. The functional source interfaces remain part of the proposed release.

## Application and database topology

```mermaid
flowchart TD
    B["Browser / PWA: HTML, CSS, vanilla JS"] --> F["FastAPI: app.py / server_main.py"]
    D["Tauri desktop client"] --> F
    F --> A["Auth, middleware, sessions and role/privilege checks"]
    A --> R["API routes"]
    R --> C["Chat agent path"]
    R --> P["Grounded answer pipeline"]
    R --> O["Council API / streamed orchestration"]
    R --> Q["Structured query builder"]
    C --> M["LLM adapter / local and optional remote providers"]
    P --> M
    O --> M
    C --> T["Tool security + agent tool dispatch"]
    P --> K["Brain / knowledge retrieval interfaces"]
    O --> X["Seat runner / fresh context / verification"]
    T --> I["Files, web, tasks, mail, calendar, MCP, model-serving tools"]
    Q --> V["Catalog validation + parameterized SELECT-only compilation"]
    V --> S["SQLAlchemy engine / session factory"]
    R --> S
    S --> PG[("PostgreSQL when DATABASE_URL configured")]
    S --> SQ[("SQLite fallback default")]
    K --> E[("Optional embeddings / ChromaDB")]
```

**Important:** These are separate route-specific processing paths, not evidence of a single universal intent router. PostgreSQL is explicitly supported via the SQLAlchemy `DATABASE_URL` connection path in `core/database.py`; SQLite is the fallback. Database-specific migration and feature parity require separate integration testing. Do not describe Prisma as a React, Express, or PostgreSQL-only application.

## Agentic loop: tool execution and feedback

```mermaid
flowchart TD
    U["User message and context"] --> L["stream_agent_loop: prompt, policy and tool registry"]
    L --> MODEL["Model stream / endpoint fallback"]
    MODEL --> DEC{"Tool action requested?"}
    DEC -- No --> OUT["Stream answer"]
    DEC -- Yes --> PARSE["Parse tool blocks or function calls"]
    PARSE --> GATE["Owner privilege and disabled-tool checks"]
    GATE --> EXEC["Execute allowed tool via dispatcher"]
    EXEC --> OBS["Format bounded tool result / error"]
    OBS --> BUDGET{"Continuation round and time budget?"}
    BUDGET -- Continue --> MODEL
    BUDGET -- Stop --> OUT
```

Implementation: `src/agent_loop.py`, `src/agent_tools.py`, `src/tool_security.py`, `src/llm_core.py`. The tool surface includes files, documents, search, Python/shell, MCP, notes, tasks, email/calendar and model management. Privileged shell execution is **not fully sandboxed**; see `THREAT_MODEL.md`.

## Grounded answer and reviewer revision loop

```mermaid
flowchart TD
    IN["Question"] --> RET["Brain / knowledge grounding retrieval"]
    RET --> CUR["Context curation, budgeting and untrusted-content fencing"]
    CUR --> DRAFT["Model drafts grounded response"]
    DRAFT --> REVIEW["Reviewer agent assesses draft vs grounding"]
    REVIEW --> OK{"Approved and grounded?"}
    OK -- Yes --> FINAL["Return approved answer and provenance"]
    OK -- No --> CHECK{"More revision rounds?"}
    CHECK -- Yes --> REFINE["Reviewer critique / revised retrieval and draft"]
    REFINE --> RET
    CHECK -- No --> STOP["Non-approved result / bounded exit"]
```

Implementation: `src/answer_pipeline.py`, `src/context_curation.py`, `src/provenance.py`, `src/prompt_security.py`, `src/verified_gate.py`. Public source can describe the mechanism without releasing private brain knowledge or training data.

## Council: DAG-based parallel specialists and synthesis

```mermaid
flowchart TD
    REQ["Council request /api/council"] --> CFG["Select per-user or default CouncilConfig"]
    CFG --> SEATS["Validate enabled seats, roles, models and consumes edges"]
    SEATS --> LAYERS["Topologically derive dependency layers"]
    LAYERS --> PAR["Run ready panelist seats concurrently within limits"]
    PAR --> UP["Collect outputs and curate upstream context"]
    UP --> NEXT{"More dependent layers?"}
    NEXT -- Yes --> AGG["Run downstream panelists / aggregators"]
    AGG --> UP
    NEXT -- No --> SYN["Streaming synthesizer seat"]
    SYN --> VERIFY["Optional verification, repair or escalation"]
    VERIFY --> SSE["SSE progress and synthesized output"]
```

Implementation: `routes/council_routes.py`, `src/council/orchestrator.py`, `src/council/schemas.py`, `src/council/seat_runner.py`, `src/council/fresh_context.py`. Seats may be panelists, aggregators or synthesizers; `consumes` defines dependencies. Independent seats in one layer execute concurrently, under timeouts and concurrency limits. Council configuration supports reasoning effort, fresh-context handling, optional code verification and escalation. The above is a conceptual control flow, not a claim that every configuration instantiates every optional stage.

## Structured query builder: separate from agent delegation

```mermaid
flowchart LR
    DEF["Saved or visual query definition"] --> API["routes/query_routes.py"]
    API --> CAT["Allowlisted catalog / schema and privilege checks"]
    CAT --> COMP["Compile parameterized SELECT-only query"]
    COMP --> RES["Optional external datasource resolver"]
    RES --> ORM["SQLAlchemy"]
    ORM --> PG[("PostgreSQL: explicit DATABASE_URL")]
    ORM --> SQ[("SQLite: default")]
```

Implementation: `routes/query_routes.py`, `src/query_catalog.py`, `src/query_compiler.py`, `src/query_external.py` and `core/database.py`. The visual query builder is a distinct subsystem from agentic model selection and tool routing.

## Capability inventory

| Domain | Functional components |
| --- | --- |
| Clients | Browser/PWA with HTML/CSS/vanilla JavaScript, desktop client with Tauri |
| Core | FastAPI, API routes, SQLAlchemy, middleware, authentication, sessions |
| Reasoning | Chat/agent loop, grounded answer pipeline, multi-agent Council DAG |
| Agent actions | Tool schema parsing, dispatch, permission checks, retries and tool-result feedback |
| Knowledge | Memory and brain interfaces, retrieval, curation, provenance, optional vector embeddings |
| Data | SQLite default, configurable PostgreSQL via `DATABASE_URL`, structured query builder |
| Integrations | Optional Ollama/llama.cpp/vLLM, other model APIs, MCP, search, mail, calendar, notification and document services |
| Security | Privilege gating, untrusted-context defense, URL restrictions, authenticated private deployment |

## Publication boundary and limitations

The public topology intentionally omits credential values, private brain corpora, conversation histories, private specialist configs, deployment addresses, internal planning, and customer-specific datasets. Omitting private **data** must not be misrepresented as removing all functional brain **code**. Do not claim that an image or topology alone can reproduce private models or datasets.

This is a source-verified component diagram. It is **not** proof that PostgreSQL migrations, every deployment variant, or the complete public export has passed independent end-to-end tests. Review `SECURITY.md`, `THREAT_MODEL.md`, `PUBLIC-RELEASE.md` and current CI before publishing.
