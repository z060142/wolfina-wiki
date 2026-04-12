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
  models/       — SQLAlchemy ORM (Page, EditProposal, ProposalReview, PageRelation,
                   PageVersion, ConversationWindow, ConversationMessage, AgentTask,
                   DirectorSession, FileIngestRecord)
  schemas/      — Pydantic request/response models
  services/     — business logic:
                   page_service, proposal_service, version_service, plugin_service,
                   agent_service, llm_service, conversation_service, scheduler_service,
                   director_service, janitor_service
  events/       — fire-and-forget async EventBus (singleton `event_bus`)
  tools/        — definitions.py (tool schemas per agent), handlers.py (tool execution)
  debug/        — event_stream.py (SSE debug stream)
  ipc/          — IPC handler for inter-process communication
  settings.py   — Pydantic-settings (reads .env); all config with defaults
api/
  app.py        — create_app() factory; exception handlers; router registration
  deps.py       — FastAPI dependency: get_db (yields AsyncSession)
  routers/      — thin HTTP layer (pages, proposals, conversations, director,
                   maintenance, ingest, query, plugins, debug)
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

### Agent architecture

Eight named agent roles, each with a distinct system prompt and tool subset:

| Agent | Role |
|---|---|
| `proposer` | Reads conversation, creates wiki proposals |
| `reviewer` | Reviews and votes approve/reject on proposals |
| `executor` | Applies approved proposals |
| `relation` | Adds semantic links between pages |
| `orchestrator` | Reads wiki state, creates AgentTask entries for specialists |
| `research` | Deep fact-gathering on a topic |
| `ingest` | Reads files, writes summaries, marks records done |
| `director` | User-facing super-agent; delegates to specialists via AgentTask |

Agent prompts are composed in `agent_service.py` using `core/services/prompt_blocks.py`. Reusable blocks (e.g. `BLOCK_ROLE_SEPARATION`, `BLOCK_SPAWN_SUBAGENTS`) are assembled via `build_prompt(role, sections, blocks)`.

### Pipelines

**Flush pipeline** (triggered by conversation thresholds or `/flush`):
```
run_flush_pipeline(conversation_text, batch_id, db)
    1. proposer  → searches wiki, creates proposals
    2. reviewer  → reviews pending proposals in this batch
    3. executor  → applies approved proposals
    4. relation  → links related pages
```

Flush pipelines have priority over maintenance — maintenance stages yield via `_wait_for_flush_gap()`.

**Maintenance pipeline** (scheduler-driven):
```
run_maintenance_pipeline(db)
    1. orchestrator → evaluates wiki state, creates AgentTask entries
    2. specialists  → each reads its tasks via list_agent_tasks, executes, calls complete_agent_task
```
Specialist agent types: `research`, `proposer`, `reviewer`, `executor`, `relation`.

**Ingest pipeline**: Triggered by `trigger_pipeline(pipeline_type="ingest")`. The orchestrator scans `FILE_READ_ALLOWED_DIRS` via `list_files`, upserts `FileIngestRecord` rows (comparing SHA-256 hashes), then the ingest agent reads each pending file and writes a summary.

### Director agent

`director_service.py` implements a persistent-history super-agent accessed via `/director/sessions`.

- Maintains per-session `messages`, `todo_list`, and `notes` as JSON in `DirectorSession`.
- `manage_todo` (max 10 active items) and `manage_note` are handled in-process (not via `dispatch_tool`) because they need mutable in-memory list references.
- Active notes are injected into the system prompt each turn.
- Cannot directly propose/review/apply — all mutations must be delegated via `create_agent_task` + `trigger_pipeline("maintenance")`.
- Streaming events (tool calls, results, delegation, pipeline triggers, final reply) are emitted via SSE at `POST /director/sessions/{id}/chat`.

### Task Janitor

`janitor_service.py` runs on its own interval (default 2 min, independent of maintenance). It handles:
1. Crashed tasks (stuck in `running` > timeout) → reset to `pending`
2. Failed tasks below retry limit → reset to `pending`
3. Duplicate pending tasks → deduplicate, keep newest
4. Stale pending tasks → nudge scheduler
5. Pipeline gaps (proposals with no matching reviewer/executor task) → create missing tasks
6. Old done/failed tasks → delete to prevent DB bloat

### Event bus

`core/events/event_bus.py` exposes a module-level singleton `event_bus`. Services call `event_bus.emit(Event(...))` after mutations. Handlers run as independent `asyncio.ensure_future` tasks — they never block the request path. Plugins subscribe in `on_load` and unsubscribe in `on_unload`.

### Plugin system

Plugins subclass `BasePlugin` and are registered via `plugin_service`. The core system never imports concrete plugin classes directly. Plugin failures are isolated from core request handling.

### LLM provider configuration (.env)

```env
# Ollama (local)
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
DEFAULT_MODEL=qwen3.5:397b-cloud

# Disable extended thinking for models that support it (e.g. Qwen3, Kimi-K2)
OLLAMA_DISABLE_THINKING=false

# OpenAI-compat (OpenRouter / LM Studio)
LLM_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=https://openrouter.ai/api/v1
OPENAI_COMPAT_API_KEY=sk-or-v1-xxx

# Per-agent model overrides (empty = use DEFAULT_MODEL)
RESEARCH_AGENT_MODEL=
PROPOSER_AGENT_MODEL=
REVIEWER_AGENT_MODEL=
EXECUTOR_AGENT_MODEL=
RELATION_AGENT_MODEL=
ORCHESTRATOR_AGENT_MODEL=
INGEST_AGENT_MODEL=
DIRECTOR_AGENT_MODEL=

# Remote access (default is localhost-only)
SERVER_HOST=127.0.0.1
SERVER_PORT=8000

# File read tool: comma-separated allowed dirs (empty = disabled)
FILE_READ_ALLOWED_DIRS=./docs,./data
```

Agent IDs (must all be distinct for role-separation checks):
`PROPOSER_AGENT_ID`, `REVIEWER_AGENT_ID`, `EXECUTOR_AGENT_ID`, `RELATION_AGENT_ID`

### Known gotchas

- **Ollama tool_call `arguments` format**: `llm_service.py` serializes `arguments` as dict for Ollama and as JSON string for `openai_compat`. Mixing this up causes `400 Bad Request` on the second LLM call in a tool loop.
- **ReviewDecision normalization**: `handlers.py` normalizes `"approved"` → `"approve"` and `"rejected"` → `"reject"` because LLMs naturally use past-tense forms even when the enum is specified.
- **Flush trigger**: `conversation_service` only triggers flush after an **assistant** message, not after a user message.
- **`manage_todo` / `manage_note`**: handled directly in `director_service.run_director_turn`, not via `dispatch_tool`, because they need mutable in-memory list references that are persisted back to the DB at the end of the turn.

### Testing

Tests use in-memory SQLite. The `client` fixture in `conftest.py` overrides `api.deps.get_db` with a test session factory. All tests are async (`asyncio_mode = "auto"` in pyproject.toml).
