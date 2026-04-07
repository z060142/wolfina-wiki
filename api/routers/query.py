"""Quick query API router.

POST /query  — drive the quick_query pipeline and return a summary.

Designed as an LLM-tool-friendly endpoint: external LLM systems (Claude API,
OpenAI function calling, MCP servers, plugins, etc.) can call this directly
without needing to be a registered internal agent.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db

router = APIRouter(prefix="/query", tags=["query"])
logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):
    query: str = Field(..., description="What to look up.")
    summary_instruction: str | None = Field(
        None,
        description=(
            "How to format the summary. "
            "E.g. 'List the key facts as bullet points.' or 'One-paragraph overview.'"
        ),
    )
    max_words: int = Field(
        150,
        ge=10,
        le=800,
        description="Hard word-count limit for the returned summary.",
    )
    allowed_tools: list[str] | None = Field(
        None,
        description=(
            "Subset of read tools to expose to the query agent. "
            "Valid values: search_pages, get_page, list_pages, "
            "get_related_pages, get_page_history, read_file, list_files. "
            "Omit to allow all."
        ),
    )


class QueryResponse(BaseModel):
    summary: str
    sources: list[str]


@router.post("", response_model=QueryResponse)
async def run_quick_query(
    body: QueryRequest,
    db: AsyncSession = Depends(get_db),
) -> QueryResponse:
    """Run the quick_query pipeline and return a summarised answer.

    This is the HTTP-accessible entry point for the same pipeline that internal
    agents call via the `quick_query` tool.  External LLM tools, MCP servers,
    plugins, and any HTTP client can use this endpoint.
    """
    from core.tools.handlers import _quick_query

    inp = {"query": body.query, "max_words": body.max_words}
    if body.summary_instruction:
        inp["summary_instruction"] = body.summary_instruction
    if body.allowed_tools is not None:
        inp["allowed_tools"] = body.allowed_tools

    result = await _quick_query(inp, db)

    if "error" in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=result["error"])

    return QueryResponse(summary=result["summary"], sources=result.get("sources", []))
