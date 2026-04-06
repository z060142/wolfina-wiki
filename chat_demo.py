"""chat_demo.py — Multi-turn CLI chat with wiki memory integration.

Usage:
    uv run python chat_demo.py
    uv run python chat_demo.py --persona persona.json
    uv run python chat_demo.py --wiki-url http://localhost:8000

How it works:
  1. Loads a persona from persona.json (LLM identity + system prompt).
  2. Creates a conversation window in the wiki system.
  3. Runs a REPL: user types → LLM responds → both messages are pushed to the wiki.
  4. The wiki accumulates messages and automatically flushes them through the
     AI agent pipeline (research → propose → review → apply) when thresholds
     are reached, turning the conversation into wiki pages.

Type 'quit' or 'exit' to end the session.
Type '/flush' to manually trigger a wiki flush right now.
Type '/status' to show the current conversation window status.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

# ── load .env values (manual parser — no dotenv dependency) ──────────────────

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip().upper()] = val.strip().strip('"').strip("'")
    return env

# ── persona ───────────────────────────────────────────────────────────────────

def load_persona(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"[error] persona file not found: {path}")
        sys.exit(1)
    with p.open(encoding="utf-8") as f:
        raw = json.load(f)
    return _normalise_persona(raw)


def _normalise_persona(raw: dict) -> dict:
    """Accept both a simple persona dict and the rich Wolfina-style persona format."""

    # Simple format: already has system_prompt
    if "system_prompt" in raw:
        return raw

    # Rich format: build system_prompt from fields
    name = raw.get("name", "Assistant")
    nickname = raw.get("nickname", "")
    core = raw.get("core_identity_summary", "")
    personality = raw.get("personality", {})
    language = raw.get("language_social", {})
    speech = raw.get("speech_dynamics", {})
    behavior = raw.get("behavior_in_situations", {})

    lines: list[str] = []

    # Core identity
    if core:
        lines.append(core)
    else:
        lines.append(f"You are {name}.")

    # Personality
    if isinstance(personality, dict):
        desc = personality.get("description", "")
        if desc:
            lines.append(f"\nPersonality: {desc}")
        strengths = personality.get("strengths", [])
        if strengths:
            lines.append("Strengths: " + ", ".join(strengths) + ".")
        weaknesses = personality.get("weaknesses", [])
        if weaknesses:
            lines.append("Weaknesses: " + ", ".join(weaknesses) + ".")

    # Language tone
    if isinstance(language, dict):
        tones = language.get("tone", [])
        if tones:
            lines.append("\nSpeech tone: " + " ".join(tones))
        patterns = language.get("verbal_patterns", [])
        if patterns:
            lines.append("Verbal patterns: " + " ".join(patterns))
        interactions = language.get("interaction_methods", [])
        if interactions:
            lines.append("Interaction style: " + " ".join(interactions))

    # Speech dynamics
    if isinstance(speech, dict):
        sigs = speech.get("lexical_signatures", [])
        if sigs:
            lines.append("Lexical signatures: " + " ".join(sigs))
        samples = speech.get("dialogue_sample", [])
        if samples:
            lines.append("Example dialogue: " + "  ".join(samples))

    # Situational behavior
    if isinstance(behavior, dict):
        parts = [f"{k}: {v}" for k, v in behavior.items()]
        if parts:
            lines.append("\nSituational behavior: " + "  ".join(parts))

    # Wiki memory instruction
    lines.append(
        "\n\nIMPORTANT: You are chatting with a user whose conversation is being "
        "automatically saved to a wiki knowledge base. Engage genuinely — share insights, "
        "ask questions, and remember what the user tells you within this session. "
        "Your conversations will eventually be processed into wiki pages as long-term memory."
    )

    greeting = raw.get("greeting", f"Hey~ it's me, {nickname or name}! What's up?")

    return {
        "name": name,
        "nickname": nickname,
        "system_prompt": "\n".join(lines),
        "greeting": greeting,
    }

# ── wiki API client ───────────────────────────────────────────────────────────

class WikiClient:
    def __init__(self, base_url: str, agent_id: str = "chat-demo") -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"X-Agent-ID": agent_id}
        self._http = httpx.Client(timeout=30.0)

    def create_window(self, source_id: str) -> dict:
        r = self._http.post(
            f"{self._base}/conversations/windows",
            json={"external_source_id": source_id},
            headers=self._headers,
        )
        r.raise_for_status()
        return r.json()

    def add_message(self, window_id: str, role: str, content: str) -> dict:
        r = self._http.post(
            f"{self._base}/conversations/windows/{window_id}/messages",
            json={"role": role, "content": content},
            headers=self._headers,
        )
        r.raise_for_status()
        return r.json()

    def flush(self, window_id: str) -> dict:
        r = self._http.post(
            f"{self._base}/conversations/windows/{window_id}/flush",
            headers=self._headers,
        )
        r.raise_for_status()
        return r.json()

    def get_window(self, window_id: str) -> dict:
        r = self._http.get(
            f"{self._base}/conversations/windows/{window_id}",
            headers=self._headers,
        )
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()

# ── LLM client (mirrors wiki's llm_service, but synchronous) ─────────────────

class LLMClient:
    def __init__(self, env: dict[str, str]) -> None:
        self._provider = env.get("LLM_PROVIDER", "ollama")
        self._model = env.get("DEFAULT_MODEL", "llama3.2")

        if self._provider == "openai_compat":
            self._base_url = env.get("OPENAI_COMPAT_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
            self._api_key = env.get("OPENAI_COMPAT_API_KEY", "")
            self._http = httpx.Client(timeout=120.0)
        else:
            # Ollama
            self._ollama_host = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
            self._http = httpx.Client(timeout=120.0)

    def chat(self, messages: list[dict]) -> str:
        if self._provider == "openai_compat":
            return self._chat_openai(messages)
        return self._chat_ollama(messages)

    def _chat_openai(self, messages: list[dict]) -> str:
        r = self._http.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self._model, "messages": messages},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _chat_ollama(self, messages: list[dict]) -> str:
        r = self._http.post(
            f"{self._ollama_host}/api/chat",
            json={"model": self._model, "messages": messages, "stream": False},
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    def close(self) -> None:
        self._http.close()

# ── formatting helpers ────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD  = "\033[1m"
CYAN  = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM   = "\033[2m"

def _color(text: str, code: str) -> str:
    if sys.stdout.isatty():
        return f"{code}{text}{RESET}"
    return text

def print_separator() -> None:
    print(_color("─" * 60, DIM))

def print_wiki_status(window: dict, flush_triggered: bool) -> None:
    msgs = window.get("message_count", "?")
    chars = window.get("total_char_count", "?")
    status = window.get("status", "?")
    line = f"[wiki] window:{window['id'][:8]}…  msgs:{msgs}  chars:{chars}  status:{status}"
    if flush_triggered:
        line += _color("  ← FLUSH TRIGGERED", YELLOW)
    print(_color(line, DIM))

# ── main REPL ─────────────────────────────────────────────────────────────────

def run_chat(wiki_url: str, persona_path: str) -> None:
    env = _load_env()
    persona = load_persona(persona_path)

    wiki = WikiClient(wiki_url)
    llm = LLMClient(env)

    # Check wiki connectivity
    try:
        window_data = wiki.create_window(source_id="chat-demo-cli")
    except httpx.ConnectError:
        print(f"[error] Cannot reach wiki at {wiki_url}")
        print("        Start the server first:  uv run uvicorn api.app:app --reload")
        llm.close()
        sys.exit(1)

    window_id = window_data["id"]

    name = persona.get("nickname") or persona.get("name", "Assistant")
    greeting = persona.get("greeting", f"Hey~ it's {name}! What's up?")
    system_prompt = persona.get("system_prompt", f"You are {name}, a helpful assistant.")

    print()
    print(_color(f"  {name} — Wiki Memory Demo", BOLD))
    print(_color(f"  Wiki window: {window_id}", DIM))
    print(_color(f"  LLM provider: {llm._provider}  model: {llm._model}", DIM))
    print()
    print_separator()
    print(_color(f"{name}: ", CYAN) + greeting)
    print_separator()

    # Push greeting as the assistant's opening message
    wiki.add_message(window_id, "assistant", greeting)

    conversation: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "assistant", "content": greeting},
    ]

    while True:
        try:
            user_input = input(_color("You: ", GREEN)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            break

        if user_input.lower() == "/flush":
            result = wiki.flush(window_id)
            print(_color(f"[wiki] Manual flush queued: {result}", YELLOW))
            continue

        if user_input.lower() == "/status":
            w = wiki.get_window(window_id)
            print_wiki_status(w, False)
            continue

        # Push user message to wiki
        wiki_resp = wiki.add_message(window_id, "user", user_input)
        flush_after_user = wiki_resp.get("flush_triggered", False)

        # Call LLM
        conversation.append({"role": "user", "content": user_input})
        try:
            reply = llm.chat(conversation)
        except Exception as exc:
            print(_color(f"[llm error] {exc}", YELLOW))
            conversation.pop()
            continue

        conversation.append({"role": "assistant", "content": reply})

        # Push assistant reply to wiki
        wiki_resp2 = wiki.add_message(window_id, "assistant", reply)
        flush_after_assistant = wiki_resp2.get("flush_triggered", False)
        flush_triggered = flush_after_user or flush_after_assistant

        print_separator()
        print(_color(f"{name}: ", CYAN) + reply)
        w = wiki.get_window(window_id)
        print_wiki_status(w, flush_triggered)
        print_separator()

    print()
    print(_color("Session ended.", DIM))
    w = wiki.get_window(window_id)
    print(f"  Final window: {w['message_count']} messages, {w['total_char_count']} chars, status={w['status']}")
    print(f"  Window ID: {window_id}")
    print()
    print(_color("Tip: To flush remaining messages to the wiki agent pipeline:", DIM))
    print(_color(f"  curl -X POST {wiki_url}/conversations/windows/{window_id}/flush", DIM))
    print()

    wiki.close()
    llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-turn chat CLI with wiki memory demo")
    parser.add_argument("--persona", default="chatexample/persona.json", help="Path to persona.json")
    parser.add_argument("--wiki-url", default="http://localhost:8000", help="Wiki API base URL")
    args = parser.parse_args()
    run_chat(args.wiki_url, args.persona)


if __name__ == "__main__":
    main()
