"""LLM client abstraction + tool-use loop.

Supports two providers, selected via settings.llm_provider:

  "ollama"        — uses the Ollama Python SDK (AsyncClient)
  "openai_compat" — uses httpx to call any OpenAI-format API
                    (OpenRouter, LM Studio, local vLLM, etc.)

The public entry-point is run_tool_loop(), which handles the full
request → tool_call → tool_result → ... → final_text cycle.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import settings
from core.tools.handlers import dispatch_tool

logger = logging.getLogger(__name__)


# ── normalised response types ─────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"   # "stop" | "tool_calls" | "length" | "error"


# ── provider clients ──────────────────────────────────────────────────────────

class OllamaClient:
    """Thin async wrapper around the Ollama Python SDK."""

    def __init__(self) -> None:
        import ollama
        headers = {}
        if settings.ollama_api_key:
            headers["Authorization"] = f"Bearer {settings.ollama_api_key}"
        self._client = ollama.AsyncClient(host=settings.ollama_host, headers=headers)

    async def chat(self, model: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        import ollama
        try:
            resp = await self._client.chat(
                model=model,
                messages=messages,
                tools=tools,
                stream=False,
            )
        except Exception as exc:
            logger.error("Ollama chat error: %s", exc)
            return LLMResponse(text=None, finish_reason="error")

        msg = resp.message
        # Determine finish reason
        if msg.tool_calls:
            finish = "tool_calls"
            calls = [
                ToolCall(
                    id=str(i),
                    name=tc.function.name,
                    arguments=tc.function.arguments if isinstance(tc.function.arguments, dict)
                              else json.loads(tc.function.arguments),
                )
                for i, tc in enumerate(msg.tool_calls)
            ]
            return LLMResponse(text=msg.content or None, tool_calls=calls, finish_reason=finish)

        return LLMResponse(text=msg.content or "", finish_reason="stop")


class OpenAICompatClient:
    """httpx-based client for any OpenAI-format API endpoint."""

    def __init__(self) -> None:
        import httpx
        self._base_url = settings.openai_compat_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {settings.openai_compat_api_key}",
            "Content-Type": "application/json",
        }
        self._http = httpx.AsyncClient(timeout=120.0)

    async def chat(self, model: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            resp = await self._http.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("OpenAI-compat chat error: %s", exc)
            return LLMResponse(text=None, finish_reason="error")

        choice = data["choices"][0]
        finish = choice.get("finish_reason", "stop")
        msg = choice["message"]

        raw_calls = msg.get("tool_calls") or []
        if raw_calls:
            calls = []
            for tc in raw_calls:
                fn = tc["function"]
                args = fn["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append(ToolCall(id=tc.get("id", ""), name=fn["name"], arguments=args))
            return LLMResponse(
                text=msg.get("content") or None,
                tool_calls=calls,
                finish_reason="tool_calls",
            )

        return LLMResponse(text=msg.get("content") or "", finish_reason=finish)


# ── singleton factory ─────────────────────────────────────────────────────────

_client_instance: OllamaClient | OpenAICompatClient | None = None


def get_client() -> OllamaClient | OpenAICompatClient:
    global _client_instance
    if _client_instance is None:
        if settings.llm_provider == "openai_compat":
            _client_instance = OpenAICompatClient()
        else:
            _client_instance = OllamaClient()
    return _client_instance


def resolve_model(agent_type: str) -> str:
    """Return the configured model for an agent type, falling back to default."""
    per_agent = {
        "research": settings.research_agent_model,
        "proposer": settings.proposer_agent_model,
        "reviewer": settings.reviewer_agent_model,
        "executor": settings.executor_agent_model,
        "relation": settings.relation_agent_model,
        "orchestrator": settings.orchestrator_agent_model,
    }
    return per_agent.get(agent_type) or settings.default_model


# ── tool-use loop ─────────────────────────────────────────────────────────────

async def run_tool_loop(
    *,
    agent_type: str,
    system_prompt: str,
    user_message: str,
    tool_definitions: list[dict],
    db: AsyncSession,
    model: str | None = None,
    max_iterations: int | None = None,
) -> str:
    """Run a full tool-use conversation loop for one agent invocation.

    Returns the final text response from the LLM (may be empty string if the
    model only produces tool calls and never emits a final text turn).

    The loop terminates when:
      - the model returns finish_reason "stop" (normal end)
      - max_iterations is reached (safety guard)
      - the model returns finish_reason "error"
    """
    client = get_client()
    resolved_model = model or resolve_model(agent_type)
    max_iter = max_iterations if max_iterations is not None else settings.agent_max_iterations

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    final_text = ""

    for iteration in range(max_iter):
        resp = await client.chat(
            model=resolved_model,
            messages=messages,
            tools=tool_definitions,
        )

        if resp.finish_reason == "error":
            logger.error("Agent %s: LLM returned error on iteration %d", agent_type, iteration)
            break

        if resp.finish_reason != "tool_calls" or not resp.tool_calls:
            # Normal end — capture text and stop.
            if resp.text:
                final_text = resp.text
            break

        # ── tool call turn ────────────────────────────────────────────────────
        # Append the assistant's tool-call message (Ollama / OpenAI format).
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.text or ""}
        assistant_msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in resp.tool_calls
        ]
        messages.append(assistant_msg)

        # Execute each tool and append results.
        for tc in resp.tool_calls:
            logger.debug("Agent %s calling tool %s with %s", agent_type, tc.name, tc.arguments)
            result = await dispatch_tool(tc.name, tc.arguments, db)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

    else:
        logger.warning("Agent %s reached max_iterations (%d)", agent_type, max_iter)

    return final_text
