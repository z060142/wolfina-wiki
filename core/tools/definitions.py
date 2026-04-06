"""Tool definitions in OpenAI function-calling format.

These are compatible with both the Ollama SDK and any OpenAI-compat API.

14 tools — each has a distinct purpose with zero functional overlap:

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
]

# Mapping from tool name → definition (for quick lookup)
TOOL_MAP: dict[str, dict] = {t["function"]["name"]: t for t in TOOLS}

# Per-agent tool subsets — each agent only sees the tools relevant to its role
AGENT_TOOLS: dict[str, list[str]] = {
    "research": [
        "search_pages", "get_page", "list_pages",
        "get_related_pages", "get_page_history",
        "list_agent_tasks", "complete_agent_task",
    ],
    "proposer": [
        "search_pages", "get_page", "list_pages",
        "propose_new_page", "propose_page_edit",
        "list_agent_tasks", "complete_agent_task",
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
    ],
}


def get_tools_for_agent(agent_type: str) -> list[dict]:
    """Return the tool definition list for the given agent type."""
    names = AGENT_TOOLS.get(agent_type, [])
    return [TOOL_MAP[n] for n in names if n in TOOL_MAP]
