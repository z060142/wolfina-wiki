"""Reusable prompt blocks for agent system prompts.

Compose agent prompts by combining a role-specific core with relevant shared blocks.
Each block is a plain string — append with a newline separator when building prompts.

Usage in agent_service.py:
    from core.services.prompt_blocks import build_prompt, BLOCK_*

    _MY_PROMPT = build_prompt(
        role="...",          # role description paragraph
        sections=[...],      # ordered list of section strings
        blocks=[             # shared blocks to append at the end
            BLOCK_AGENT_TASK_WORKFLOW,
            BLOCK_SPAWN_SUBAGENTS,
        ],
    )
"""

from __future__ import annotations


# ── helpers ───────────────────────────────────────────────────────────────────

def build_prompt(
    role: str,
    sections: list[str],
    blocks: list[str],
) -> str:
    """Assemble a system prompt from parts.

    Args:
        role:     Opening paragraph describing who the agent is and what it does.
        sections: Ordered list of mode/step/guideline sections (each a multi-line string).
        blocks:   Shared blocks appended after all sections (behaviour rules, tool tips, etc.).
    """
    parts = [role.strip()]
    for s in sections:
        parts.append(s.strip())
    for b in blocks:
        parts.append(b.strip())
    return "\n\n".join(parts) + "\n"


# ── shared blocks ─────────────────────────────────────────────────────────────

BLOCK_AGENT_TASK_WORKFLOW = """\
== AGENT TASK WORKFLOW ==
Specialist agents follow this pattern in MAINTENANCE MODE:
1. Call list_agent_tasks (filter by your agent_type and the provided batch_id) to find pending tasks.
2. Execute each task using your available tools.
3. Call complete_agent_task with outcome="done" or outcome="failed" (include error_message if failed).
4. If there are no tasks assigned to you, do nothing and stop."""

BLOCK_NO_DUPLICATE_TASKS = """\
== DUPLICATE PREVENTION ==
Before creating any task or proposal, check whether an equivalent one already exists:
- Use list_agent_tasks to check pending/running tasks before calling create_agent_task.
- Use search_pages before calling propose_new_page — prefer editing an existing page over creating a duplicate.
- Use list_proposals to check for existing pending proposals on the same page before proposing again."""

BLOCK_PROPOSAL_GUIDELINES = """\
== PROPOSAL GUIDELINES ==
- Prefer editing existing pages (propose_page_edit) over creating new ones (propose_new_page).
- Always search first — call search_pages before deciding to create a new page.
- Each proposal must include a clear, specific rationale.
- Be conservative: only propose content that is clearly supported by the source material.
- One pending proposal per page at a time — do not stack multiple proposals on the same page."""

BLOCK_ROLE_SEPARATION = """\
== ROLE SEPARATION (enforced by the system) ==
- The Proposer, Reviewer, and Executor must all be different agents.
- A reviewer cannot review their own proposals.
- The executor cannot apply proposals they proposed or reviewed.
- Violating these rules will result in a 403 error from the API."""

BLOCK_SPAWN_SUBAGENTS = """\
== SPAWN SUBAGENTS — parallel read-only research ==
When you need to gather information from multiple independent sources simultaneously,
use spawn_subagents instead of making sequential tool calls yourself.

When to use:
- You need to look up 2 unrelated topics, pages, or files at the same time.
- Parallel lookups would save time and reduce your context usage.

How to use:
- Provide 1–2 tasks. Each task needs a task_id (short label) and a self-contained instruction.
- Subagents have NO access to your context — include all necessary details (page IDs, slugs,
  file paths, search terms) directly in the instruction or context field.
- Subagents are read-only: search_pages, get_page, list_pages, get_related_pages,
  get_page_history, read_file, list_files. They cannot write, propose, or create tasks.
- Results are returned to you as {"subagent_results": [{"task_id": ..., "result": ...}, ...]}.

When NOT to use:
- Tasks that depend on each other's output (run them sequentially instead).
- Tasks that require write access (do those yourself).
- When a single tool call is sufficient."""

BLOCK_FILE_READ_PAGINATION = """\
== READING LARGE FILES ==
Do not attempt to read an entire large file in one call.
- Call read_file without offset_lines first to get total_lines and the first page.
- If has_more is true, call again with offset_lines to read subsequent pages.
- Use search_pattern to jump directly to relevant sections when you know what to look for.
- For discovery, use list_files first to find available files before reading them."""

BLOCK_RELATION_TYPES = """\
== PAGE RELATION TYPES ==
- parent / child : hierarchical containment (e.g. "Python" is parent of "Python Basics")
- related_to     : semantically related pages at the same level (bidirectional)
- references     : one page cites or is derived from another (directional)
Always call get_related_pages before adding a relation — skip any that already exist.
If add_page_relation returns an "already exists" error, do NOT retry."""

BLOCK_IDEMPOTENCY = """\
== SAFE RETRIES (idempotency) ==
When submitting proposals that may be retried (e.g. after a transient error), pass an
idempotency_key. Re-submitting the same key returns the existing proposal instead of
creating a duplicate. Use a deterministic key based on the content (e.g. slug + batch_id)."""
