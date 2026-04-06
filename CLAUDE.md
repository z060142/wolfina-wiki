# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (including dev extras)
uv sync --extra dev

# Run the API server
uv run uvicorn api.app:app --reload

# Run chat demo (interactive CLI)
uv run python chat_demo.py --persona chatexample/persona.json
# In chat: /flush forces the flush pipeline immediately

# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_proposals.py

# Run a single test by name
uv run pytest tests/test_proposals.py::test_name -v

# Watch debug event stream (SSE) while pipeline runs
curl -N http://localhost:8000/debug/stream

# Query wiki pages directly
sqlite3 wolfina.db "SELECT title, slug FROM pages"
```

## Architecture

This is a **FastAPI + SQLAlchemy async** application. The DB auto-creates on startup (no Alembic migrations needed yet — `Base.metadata.create_all` runs in the lifespan hook).

### Layer structure

```
core/           — domain logic, no FastAPI imports
  models/       — SQLAlchemy ORM (Page, EditProposal, ProposalReview, PageRelation, PageVersion)
  schemas/      — Pydantic request/response models
  services/     — business logic (page_service, proposal_service, version_service, plugin_service,
                   agent_service, llm_service, conversation_service, scheduler_service)
  events/       — fire-and-forget async EventBus (singleton `event_bus`)
  tools/        — definitions.py (tool schemas per agent), handlers.py (tool execution)
  debug/        — event_stream.py (SSE debug stream)
  ipc/          — IPC handler for inter-process communication
  settings.py   — Pydantic-settings (reads .env); key config: database_url, min_reviewers,
                   llm_provider, agent IDs
api/
  app.py        — create_app() factory; exception handlers; router registration
  deps.py       — FastAPI dependency: get_db (yields AsyncSession)
  routers/      — thin HTTP layer; delegates all logic to core/services
plugins/
  base_plugin.py — BasePlugin ABC: on_load(event_bus), on_unload(), name, version, capabilities
```

### Proposal workflow (multi-agent governance)

The core feature. Changes to wiki pages go through: **propose → review → apply**.

Role-separation rules enforced in `proposal_service`:
- Proposer ≠ Reviewer
- Executor ≠ Proposer AND Executor ≠ any Reviewer
- A reviewer cannot vote twice on the same proposal
- An agent can only have one `pending` proposal per page at a time

`min_reviewers` (default 1) controls how many `approve` votes are needed before a proposal becomes `approved`. Any `reject` vote immediately rejects it.

`apply_proposal` uses `SELECT ... FOR UPDATE` to serialize concurrent apply attempts.

**Idempotency**: `ProposalCreate` accepts an optional `idempotency_key`; re-submitting the same key returns the existing proposal without creating a duplicate (safe for agent retries).

**Batch traceability**: proposals carry optional `batch_id` and `source_session_id` for grouping multi-proposal reasoning runs.

### Event bus

`core/events/event_bus.py` exposes a module-level singleton `event_bus`. Services call `event_bus.emit(Event(...))` after mutations. Handlers run as independent `asyncio.ensure_future` tasks — they never block the request path. Plugins subscribe in `on_load` and unsubscribe in `on_unload`.

### Plugin system

Plugins subclass `BasePlugin` and are registered via `plugin_service`. The core system never imports concrete plugin classes directly. Plugin failures are isolated from core request handling.

### Maintenance pipeline

Runs on a scheduler (`scheduler_service`). Separate from the flush pipeline:

```
run_maintenance_pipeline(db)
    1. orchestrator → evaluates wiki state, creates AgentTask entries
    2. specialists  → each reads its tasks via list_agent_tasks, executes, calls complete_agent_task
```

Specialist agent types: `research`, `proposer`, `reviewer`, `executor`, `relation`.

### LLM provider configuration (.env)

```env
# Ollama (local)
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
DEFAULT_MODEL=kimi-k2.5:cloud

# OpenAI-compat (OpenRouter / LM Studio)
LLM_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=https://openrouter.ai/api/v1
OPENAI_COMPAT_API_KEY=sk-or-v1-xxx
```

Agent IDs (must all be distinct for role-separation checks):
`PROPOSER_AGENT_ID`, `REVIEWER_AGENT_ID`, `EXECUTOR_AGENT_ID`, `RELATION_AGENT_ID`

### Known gotchas

- **Ollama tool_call `arguments` format**: `llm_service.py` serializes `arguments` as dict for Ollama and as JSON string for `openai_compat`. Mixing this up causes `400 Bad Request` on the second LLM call in a tool loop.
- **ReviewDecision normalization**: `handlers.py` normalizes `"approved"` → `"approve"` and `"rejected"` → `"reject"` because LLMs naturally use past-tense forms even when the enum is specified.
- **Flush trigger**: `conversation_service` only triggers flush after an **assistant** message, not after a user message.

### Testing

Tests use in-memory SQLite. The `client` fixture in `conftest.py` overrides `api.deps.get_db` with a test session factory. All tests are async (`asyncio_mode = "auto"` in pyproject.toml).
