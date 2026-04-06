"""Tool definitions in OpenAI function-calling format.

These are compatible with both the Ollama SDK and any OpenAI-compat API.

19 tools — each has a distinct purpose with zero functional overlap:

  Read-only (wiki):
    1. search_pages       — keyword search across title/content/summary
    2. get_page           — fetch one page by id or slug
    3. list_pages         — browse pages without a keyword query
    4. get_related_pages  — fetch pages connected by any relation
    5. get_page_history   — fetch version list for a page

  Proposal flow:
    6. list_proposals     — list proposals with status/page filters
    7. propose_new_page   — submit a creation proposal (no existing page)
    8. propose_page_edit  — submit an edit proposal for an existing page
    9. review_proposal    — approve or reject a pending proposal
   10. apply_proposal     — apply an approved proposal (executor only)

  Relations:
   11. add_page_relation  — add a directional relation between two pages

  Task management (orchestrator ↔ specialist agents):
   12. create_agent_task  — orchestrator creates a work item for a specialist
   13. list_agent_tasks   — list tasks filtered by status / agent_type
   14. complete_agent_task — specialist marks its own task done or failed

  File system (read-only, restricted):
   15. read_file          — read a file from an allowed directory (with pagination and regex search)
   16. list_files         — list/search files by glob pattern within allowed directories

  Ingest pipeline:
   17. list_ingest_records  — query FileIngestRecord entries (status, path filter)
   18. complete_file_ingest — ingest agent writes summary + marks file done/failed

  Subagent delegation:
   19. spawn_subagents    — run up to 2 isolated read-only subagents in parallel
"""

from __future__ import annotations

# Each entry: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

TOOLS: list[dict] = [
    # ── 1 ── search_pages ────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "search_pages",
            "description": (
                "Search wiki pages by keyword. Matches against title, content, and summary. "
                "Use this when you have a specific term to look up. "
                "For browsing all pages without a query, use list_pages instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword or phrase to search for.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "archived"],
                        "description": "Filter by page status. Defaults to active.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results. Default 10, max 50.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    # ── 2 ── get_page ─────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_page",
            "description": (
                "Retrieve a single wiki page by its UUID or slug. "
                "Use slug when you know the page's URL-friendly name; use page_id when you have the UUID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Page UUID (mutually exclusive with slug).",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Page slug, e.g. 'python-basics' (mutually exclusive with page_id).",
                    },
                },
            },
        },
    },
    # ── 3 ── list_pages ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_pages",
            "description": (
                "Browse wiki pages without a keyword query. "
                "Useful for auditing, finding recently updated pages, or enumerating all entries. "
                "To search by keyword, use search_pages instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "archived"],
                        "description": "Filter by status. Omit to return all statuses.",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["updated_at", "created_at", "title"],
                        "description": "Sort field. Default: updated_at.",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": "Sort direction. Default: desc.",
                    },
                    "limit": {"type": "integer", "description": "Max results. Default 20."},
                    "offset": {"type": "integer", "description": "Pagination offset. Default 0."},
                },
            },
        },
    },
    # ── 4 ── get_related_pages ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_related_pages",
            "description": (
                "Return pages connected to the given page by any outgoing relation "
                "(parent, child, related_to, references). "
                "Does not perform a keyword search — use search_pages for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "UUID of the source page.",
                    },
                },
                "required": ["page_id"],
            },
        },
    },
    # ── 5 ── get_page_history ─────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "get_page_history",
            "description": (
                "Return the version history of a page in chronological order. "
                "Each version shows the content snapshot, editor agent, and reason for the edit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "UUID of the page.",
                    },
                },
                "required": ["page_id"],
            },
        },
    },
    # ── 6 ── list_proposals ───────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_proposals",
            "description": (
                "List edit proposals with optional filters. "
                "Use status='pending' to find proposals awaiting review, "
                "or status='approved' to find proposals ready to apply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "rejected", "applied", "cancelled"],
                    },
                    "page_id": {
                        "type": "string",
                        "description": "Filter to proposals targeting this page UUID.",
                    },
                    "batch_id": {
                        "type": "string",
                        "description": "Filter to proposals from a specific processing batch.",
                    },
                    "limit": {"type": "integer", "description": "Max results. Default 20."},
                },
            },
        },
    },
    # ── 7 ── propose_new_page ─────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "propose_new_page",
            "description": (
                "Submit a proposal to create a new wiki page. "
                "The page will not be created until the proposal is reviewed and approved. "
                "Use propose_page_edit when the page already exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Page title."},
                    "slug": {
                        "type": "string",
                        "description": "URL-friendly identifier (lowercase, hyphens). Must be unique.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full page content in Markdown.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-paragraph plain-text summary.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this page should be created and what information it captures.",
                    },
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional source references (URLs, doc IDs, etc.).",
                    },
                    "proposer_agent_id": {
                        "type": "string",
                        "description": "ID of the agent submitting this proposal.",
                    },
                    "batch_id": {
                        "type": "string",
                        "description": "Optional batch identifier for grouping related proposals.",
                    },
                    "idempotency_key": {
                        "type": "string",
                        "description": "Optional key for safe retries — re-submitting the same key returns the existing proposal.",
                    },
                },
                "required": ["title", "slug", "content", "summary", "rationale", "proposer_agent_id"],
            },
        },
    },
    # ── 8 ── propose_page_edit ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "propose_page_edit",
            "description": (
                "Submit a proposal to edit an existing wiki page. "
                "The page will not change until the proposal is reviewed and approved. "
                "Use propose_new_page when the page does not exist yet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_page_id": {
                        "type": "string",
                        "description": "UUID of the page to edit.",
                    },
                    "proposed_title": {
                        "type": "string",
                        "description": "New title (omit to keep existing).",
                    },
                    "proposed_content": {
                        "type": "string",
                        "description": "Full replacement content in Markdown.",
                    },
                    "proposed_summary": {
                        "type": "string",
                        "description": "New plain-text summary.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Reason for the edit.",
                    },
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "proposer_agent_id": {
                        "type": "string",
                        "description": "ID of the agent submitting this proposal.",
                    },
                    "batch_id": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                },
                "required": [
                    "target_page_id",
                    "proposed_content",
                    "proposed_summary",
                    "rationale",
                    "proposer_agent_id",
                ],
            },
        },
    },
    # ── 9 ── review_proposal ──────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "review_proposal",
            "description": (
                "Approve or reject a pending edit proposal. "
                "The reviewer must be a different agent than the proposer. "
                "A rejection immediately closes the proposal; an approval may trigger apply eligibility."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string", "description": "UUID of the proposal to review."},
                    "reviewer_agent_id": {
                        "type": "string",
                        "description": "ID of the reviewing agent (must differ from proposer).",
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "reject"],
                    },
                    "feedback": {
                        "type": "string",
                        "description": "Optional explanation of the decision.",
                    },
                },
                "required": ["proposal_id", "reviewer_agent_id", "decision"],
            },
        },
    },
    # ── 10 ── apply_proposal ──────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "apply_proposal",
            "description": (
                "Apply an approved proposal to make the wiki change official. "
                "Only call this after the proposal status is 'approved'. "
                "The executor must differ from both the proposer and all reviewers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal_id": {"type": "string", "description": "UUID of the approved proposal."},
                    "executor_agent_id": {
                        "type": "string",
                        "description": "ID of the executing agent.",
                    },
                },
                "required": ["proposal_id", "executor_agent_id"],
            },
        },
    },
    # ── 11 ── add_page_relation ───────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "add_page_relation",
            "description": (
                "Add a directional relation between two wiki pages. "
                "Relation types: parent (source is parent of target), child (source is child of target), "
                "related_to (bidirectional semantic link), references (source cites target)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_page_id": {"type": "string", "description": "UUID of the source page."},
                    "target_page_id": {"type": "string", "description": "UUID of the target page."},
                    "relation_type": {
                        "type": "string",
                        "enum": ["parent", "child", "related_to", "references"],
                    },
                    "created_by_agent": {
                        "type": "string",
                        "description": "ID of the agent adding the relation.",
                    },
                },
                "required": ["source_page_id", "target_page_id", "relation_type", "created_by_agent"],
            },
        },
    },
    # ── 12 ── create_agent_task ───────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "create_agent_task",
            "description": (
                "Create a work item for a specialist agent. "
                "Only the orchestrator agent should call this. "
                "Specialist agents consume tasks via list_agent_tasks and complete them via complete_agent_task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_type": {
                        "type": "string",
                        "enum": ["research", "proposer", "reviewer", "executor", "relation"],
                        "description": "Which specialist agent should handle this task.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Clear instruction for the agent describing what to do.",
                    },
                    "context_json": {
                        "type": "string",
                        "description": "Optional JSON string with additional context (page IDs, search terms, etc.).",
                    },
                    "batch_id": {
                        "type": "string",
                        "description": "Optional batch identifier to group related tasks.",
                    },
                },
                "required": ["agent_type", "instruction"],
            },
        },
    },
    # ── 13 ── list_agent_tasks ────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_agent_tasks",
            "description": (
                "List agent tasks with optional filters. "
                "Specialist agents use this to find their pending tasks. "
                "The orchestrator uses this to monitor work queue status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "running", "done", "failed"],
                    },
                    "agent_type": {
                        "type": "string",
                        "enum": ["research", "proposer", "reviewer", "executor", "relation"],
                    },
                    "batch_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max results. Default 20."},
                },
            },
        },
    },
    # ── 14 ── complete_agent_task ─────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "complete_agent_task",
            "description": (
                "Mark an agent task as done or failed. "
                "Call this at the end of processing a task obtained from list_agent_tasks. "
                "Only specialist agents should call this (not the orchestrator)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "UUID of the task to update."},
                    "outcome": {
                        "type": "string",
                        "enum": ["done", "failed"],
                    },
                    "error_message": {
                        "type": "string",
                        "description": "Required when outcome is 'failed'. Describes what went wrong.",
                    },
                },
                "required": ["task_id", "outcome"],
            },
        },
    },
    # ── 15 ── read_file ───────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the text content of a file from the filesystem. "
                "Only files within the administrator-configured allowed directories can be accessed. "
                "Supports line-based pagination for large files and regex search within the file. "
                "Use this to read reference documents, configuration files, or source material "
                "that should inform wiki content. Binary files are not supported.\n\n"
                "Workflow for large files:\n"
                "  1. Call without offset_lines/max_lines to get the first page and see total_lines.\n"
                "  2. If has_more is true, call again with offset_lines to read subsequent pages.\n"
                "  3. Alternatively, use search_pattern to find specific sections without reading everything."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the file. Can be absolute or relative. "
                            "Relative paths are resolved from the process working directory or the allowed directories. "
                            "Must resolve to a location inside an allowed directory."
                        ),
                    },
                    "encoding": {
                        "type": "string",
                        "description": "Text encoding. Defaults to 'utf-8'. Use 'utf-8-sig' for UTF-8-BOM files.",
                    },
                    "offset_lines": {
                        "type": "integer",
                        "description": "0-based line number to start reading from. Default 0 (beginning of file).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum number of lines to return per call. Default 200, max 1000.",
                    },
                    "search_pattern": {
                        "type": "string",
                        "description": (
                            "Optional regex pattern to search within the file. "
                            "When provided, only matching lines are returned (with line numbers and context). "
                            "offset_lines and max_lines are ignored when this is set."
                        ),
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": (
                            "Number of lines before and after each search match to include as context. "
                            "Only used with search_pattern. Default 2."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
    },
    # ── 16 ── list_files ──────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List or search for files within the allowed directories. "
                "Supports glob patterns (e.g. '**/*.md', 'docs/*.txt') to find files by name or extension. "
                "Use this before read_file when you need to discover what files are available. "
                "Only files within the administrator-configured allowed directories are returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern to match against. Examples: '**/*.md' (all Markdown files), "
                            "'*.txt' (text files in root of each allowed dir), 'docs/**' (everything under docs/). "
                            "Defaults to '**/*' (all files in all allowed directories)."
                        ),
                    },
                    "base_dir": {
                        "type": "string",
                        "description": (
                            "Optional subdirectory within an allowed directory to search in. "
                            "Narrows the search scope. Must resolve inside an allowed directory."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results. Default 50, max 200.",
                    },
                },
            },
        },
    },
    # ── 17 ── list_ingest_records ─────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_ingest_records",
            "description": (
                "Query the file ingest tracking records. "
                "Use this to see which files have been processed, which are pending, "
                "and to read per-file summaries for cross-file planning. "
                "The orchestrator uses this to plan wiki page groupings across multiple files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "processing", "done", "failed"],
                        "description": "Filter by processing status. Omit to return all.",
                    },
                    "path_contains": {
                        "type": "string",
                        "description": "Optional substring filter on the file path.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results. Default 50.",
                    },
                },
            },
        },
    },
    # ── 19 ── spawn_subagents ─────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "spawn_subagents",
            "description": (
                "Spawn up to 2 isolated read-only subagents that run in parallel. "
                "Each subagent receives its own instruction and optional context data, "
                "executes independently (no shared state), and returns a text result. "
                "Use this to parallelise research or information-gathering tasks that "
                "do not require write access, saving context window and time. "
                "Subagents can only use: search_pages, get_page, list_pages, "
                "get_related_pages, get_page_history, read_file, list_files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "List of 1–2 tasks to run in parallel.",
                        "minItems": 1,
                        "maxItems": 2,
                        "items": {
                            "type": "object",
                            "properties": {
                                "task_id": {
                                    "type": "string",
                                    "description": (
                                        "A short caller-defined label to identify this task "
                                        "in the results (e.g. 'check_python_page')."
                                    ),
                                },
                                "instruction": {
                                    "type": "string",
                                    "description": (
                                        "Full, self-contained instruction for the subagent. "
                                        "Include everything the subagent needs — it has no "
                                        "access to the parent agent's context."
                                    ),
                                },
                                "context": {
                                    "type": "string",
                                    "description": (
                                        "Optional extra data to pass to the subagent "
                                        "(e.g. page IDs, search terms, file paths). "
                                        "Will be appended to the instruction."
                                    ),
                                },
                            },
                            "required": ["task_id", "instruction"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    # ── 18 ── complete_file_ingest ────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "complete_file_ingest",
            "description": (
                "Called by the ingest agent after processing a file. "
                "Writes a content summary (for future cross-file orchestration), "
                "records which wiki pages were created/updated from this file, "
                "and marks the record as done or failed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "UUID of the FileIngestRecord to update.",
                    },
                    "outcome": {
                        "type": "string",
                        "enum": ["done", "failed"],
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "A concise description of what this file contains "
                            "(topics, key facts, structure). Used by the orchestrator "
                            "for future cross-file planning without re-reading the raw file."
                        ),
                    },
                    "related_page_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "UUIDs of wiki pages that were proposed or updated from this file.",
                    },
                    "error_message": {
                        "type": "string",
                        "description": "Required when outcome is 'failed'. Describes what went wrong.",
                    },
                },
                "required": ["record_id", "outcome"],
            },
        },
    },
]

# Mapping from tool name → definition (for quick lookup)
TOOL_MAP: dict[str, dict] = {t["function"]["name"]: t for t in TOOLS}

# Per-agent tool subsets — each agent only sees the tools relevant to its role
AGENT_TOOLS: dict[str, list[str]] = {
    "research": [
        "search_pages", "get_page", "list_pages",
        "get_related_pages", "get_page_history",
        "list_agent_tasks", "complete_agent_task",
        "read_file", "list_files",
        "spawn_subagents",
    ],
    "proposer": [
        "search_pages", "get_page", "list_pages",
        "propose_new_page", "propose_page_edit",
        "list_agent_tasks", "complete_agent_task",
        "spawn_subagents",
    ],
    "reviewer": [
        "search_pages", "get_page", "list_pages", "get_page_history",
        "list_proposals", "review_proposal",
        "list_agent_tasks", "complete_agent_task",
    ],
    "executor": [
        "list_proposals", "apply_proposal",
        "list_agent_tasks", "complete_agent_task",
    ],
    "relation": [
        "get_page", "list_pages", "search_pages", "get_related_pages",
        "add_page_relation",
        "list_agent_tasks", "complete_agent_task",
    ],
    "orchestrator": [
        "search_pages", "get_page", "list_pages",
        "list_proposals", "list_agent_tasks",
        "create_agent_task",
        "list_files", "list_ingest_records",
        "spawn_subagents",
    ],
    "ingest": [
        "list_agent_tasks", "complete_agent_task",
        "list_files", "read_file",
        "list_ingest_records", "complete_file_ingest",
        "search_pages", "list_pages",
        "create_agent_task",
        "spawn_subagents",
    ],
}


def get_tools_for_agent(agent_type: str) -> list[dict]:
    """Return the tool definition list for the given agent type."""
    names = AGENT_TOOLS.get(agent_type, [])
    return [TOOL_MAP[n] for n in names if n in TOOL_MAP]
