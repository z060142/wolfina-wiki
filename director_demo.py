"""director_demo.py — Interactive CLI for the Director super-agent.

Usage:
    uv run python director_demo.py
    uv run python director_demo.py --session <session-id>   # resume existing session
    uv run python director_demo.py --host localhost --port 8000
    uv run python director_demo.py --wiki-url http://localhost:8000  # legacy

Input:
    Enter       — submit message
    Alt+Enter   — insert a newline (multi-line input)

Special commands:
    /sessions        — list all director sessions
    /new             — start a fresh session
    /todo            — show the director's current todo list
    /status          — show session info
    /config          — show current host / port
    /config host <h> — change host (reconnects)
    /config port <p> — change port (reconnects)
    quit / exit      — end the demo
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

# ── ANSI colours (for non-prompt_toolkit output) ──────────────────────────────
RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
CYAN    = "\033[36m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
BLUE    = "\033[34m"
MAGENTA = "\033[35m"
RED     = "\033[31m"
WHITE   = "\033[37m"


def _c(text: str, *codes: str) -> str:
    if sys.stdout.isatty():
        return "".join(codes) + text + RESET
    return text


SEP = _c("─" * 70, DIM)

# ── prompt_toolkit style ───────────────────────────────────────────────────────
_PT_STYLE = Style.from_dict({
    "prompt":  "ansigreen bold",
    "toolbar": "bg:#333333 #aaaaaa",
    "toolbar.key": "bg:#333333 #ffcc00 bold",
})


def _make_key_bindings() -> KeyBindings:
    """Enter = submit; Alt+Enter = insert newline."""
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")  # Alt+Enter on most terminals
    def _newline(event):
        event.current_buffer.insert_text("\n")

    return kb


# ── HTTP client ────────────────────────────────────────────────────────────────

class DirectorClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._http = httpx.Client(timeout=300.0)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def create_session(self, title: str = "Director Session") -> dict:
        r = self._http.post(f"{self.base_url}/director/sessions", json={"title": title})
        r.raise_for_status()
        return r.json()

    def list_sessions(self) -> list[dict]:
        r = self._http.get(f"{self.base_url}/director/sessions")
        r.raise_for_status()
        return r.json()["sessions"]

    def get_session(self, session_id: str) -> dict:
        r = self._http.get(f"{self.base_url}/director/sessions/{session_id}")
        r.raise_for_status()
        return r.json()

    def chat(self, session_id: str, message: str) -> None:
        """Stream chat events and display them in real time."""
        with self._http.stream(
            "POST",
            f"{self.base_url}/director/sessions/{session_id}/chat",
            json={"message": message},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                _render_event(evt)

    def close(self) -> None:
        self._http.close()


# ── event rendering ────────────────────────────────────────────────────────────

_TOOL_ICONS = {
    "search_pages":        "search",
    "get_page":            "get_page",
    "list_pages":          "list_pages",
    "get_related_pages":   "related",
    "get_page_history":    "history",
    "list_proposals":      "proposals",
    "read_file":           "read_file",
    "list_files":          "list_files",
    "list_ingest_records": "ingest_records",
    "create_agent_task":   "delegate",
    "list_agent_tasks":    "task_queue",
    "spawn_subagents":     "subagents",
    "trigger_pipeline":    "pipeline",
    "manage_todo":         "todo",
    "output_md_write":     "write_md",
    "output_md_copy_page": "copy_page_md",
    "output_md_copy_task": "copy_task_md",
    "output_md_list":      "list_md",
}

_AGENT_COLOURS = {
    "research": CYAN,
    "proposer": BLUE,
    "reviewer": YELLOW,
    "executor": GREEN,
    "relation": MAGENTA,
    "ingest":   WHITE,
}


def _render_event(evt: dict) -> None:
    t = evt.get("type", "")

    if t == "thinking":
        iteration = evt.get("iteration", 0)
        if iteration == 0:
            print(_c("  [director] thinking…", DIM))

    elif t == "tool_call":
        tool = evt.get("tool", "?")
        args = evt.get("args", {})
        label = _TOOL_ICONS.get(tool, tool)
        args_parts = []
        for k, v in args.items():
            s = str(v)
            if len(s) > 60:
                s = s[:57] + "…"
            args_parts.append(f"{k}={s!r}")
        args_str = ", ".join(args_parts) if args_parts else ""
        print(_c(f"  [tool] {label}({args_str})", DIM))

    elif t == "delegate":
        agent = evt.get("agent_type", "?")
        instruction = evt.get("instruction", "")
        colour = _AGENT_COLOURS.get(agent, WHITE)
        print()
        print(_c(f"  >> DELEGATING to [{agent}]", BOLD, colour))
        print(_c(f"     {instruction}", colour))
        print()

    elif t == "pipeline":
        pt = evt.get("pipeline_type", "?")
        print(_c(f"  >> FIRING pipeline: {pt}", BOLD, YELLOW))

    elif t == "tool_result":
        tool = evt.get("tool", "?")
        ok = evt.get("ok", True)
        preview = evt.get("preview", "")
        status = _c("ok", GREEN) if ok else _c("ERR", RED)
        print(_c(f"  [result:{status}] {tool}: {preview}", DIM))

    elif t == "reply":
        text = evt.get("text", "")
        print()
        print(SEP)
        print(_c("Director: ", BOLD, CYAN) + text)

    elif t == "error":
        msg = evt.get("message", "unknown error")
        print(_c(f"  [ERROR] {msg}", RED))

    elif t == "done":
        print(SEP)


# ── interactive REPL ───────────────────────────────────────────────────────────

def _pick_or_create_session(client: DirectorClient, session_id: str | None) -> dict:
    if session_id:
        try:
            session = client.get_session(session_id)
            print(_c(f"  Resumed session: {session['title']} [{session_id[:8]}…]", DIM))
            return session
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(_c(f"  Session {session_id!r} not found. Starting a new one.", YELLOW))
            else:
                raise

    session = client.create_session()
    print(_c(f"  Created session: {session['id'][:8]}…", DIM))
    return session


def run_demo(host: str, port: int, session_id: str | None) -> None:
    client = DirectorClient(host, port)

    try:
        session = _pick_or_create_session(client, session_id)
    except httpx.ConnectError:
        print(f"[error] Cannot reach wiki at {client.base_url}")
        print("        Start the server first:  uv run uvicorn api.app:app --reload")
        sys.exit(1)

    sid = session["id"]

    print()
    print(SEP)
    print(_c("  Wolfina Wiki — Director Agent", BOLD))
    print(_c(f"  Session ID : {sid}", DIM))
    print(_c(f"  Wiki URL   : {client.base_url}", DIM))
    print()
    print(_c("  The Director reads everything, plans with a todo list, and delegates", DIM))
    print(_c("  wiki changes to specialist agents.  It will NOT edit pages directly.", DIM))
    print(SEP)
    print()
    print(_c("  Commands: /sessions  /new  /todo  /status  /config  quit", DIM))
    print(_c("  Input:    Enter = submit  |  Alt+Enter = new line", DIM))
    print()

    kb = _make_key_bindings()

    def _toolbar() -> HTML:
        return HTML(
            f" <b class='toolbar.key'>Session</b> {sid[:8]}… "
            f"| <b class='toolbar.key'>URL</b> {client.base_url} "
            f"| <b class='toolbar.key'>Alt+Enter</b> new line "
        )

    prompt_session: PromptSession = PromptSession(
        multiline=True,
        key_bindings=kb,
        bottom_toolbar=_toolbar,
        style=_PT_STYLE,
        prompt_continuation="... ",
    )

    while True:
        try:
            user_input = prompt_session.prompt(
                HTML("<ansigreen><b>You</b></ansigreen>: "),
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            break

        # ── built-in commands ──────────────────────────────────────────────────
        if user_input.lower() == "/sessions":
            sessions = client.list_sessions()
            if not sessions:
                print(_c("  No sessions found.", DIM))
            else:
                print(_c(f"  {len(sessions)} session(s):", YELLOW))
                for s in sessions:
                    marker = " *" if s["id"] == sid else "  "
                    print(_c(f"{marker} [{s['id'][:8]}…] {s['title']}  ({s['message_count']} msgs)", DIM))
            continue

        if user_input.lower() == "/new":
            session = client.create_session()
            sid = session["id"]
            print(_c(f"  Started new session: {sid[:8]}…", YELLOW))
            continue

        if user_input.lower() == "/todo":
            s = client.get_session(sid)
            todos = s.get("todo_list", [])
            if not todos:
                print(_c("  Todo list is empty.", DIM))
            else:
                print(_c(f"  Todo list ({len(todos)} items):", YELLOW))
                for t in todos:
                    done_mark = _c("[x]", GREEN) if t.get("done") else _c("[ ]", DIM)
                    print(f"    {done_mark} #{t['id']} {t['text']}")
            continue

        if user_input.lower() == "/status":
            s = client.get_session(sid)
            print(_c(f"  Session : {s['id'][:8]}…  {s['title']}", YELLOW))
            print(_c(f"  Messages: {s['message_count']}", DIM))
            print(_c(f"  Updated : {s.get('updated_at', '?')}", DIM))
            todo_done = sum(1 for t in s.get("todo_list", []) if t.get("done"))
            todo_total = len(s.get("todo_list", []))
            print(_c(f"  Todo    : {todo_done}/{todo_total} done", DIM))
            continue

        if user_input.lower().startswith("/config"):
            parts = user_input.split()
            if len(parts) == 1:
                # Show current config
                print(_c(f"  Host : {client.host}", YELLOW))
                print(_c(f"  Port : {client.port}", YELLOW))
                print(_c(f"  URL  : {client.base_url}", DIM))
                print(_c("  Usage: /config host <host>  |  /config port <port>", DIM))
            elif len(parts) == 3 and parts[1].lower() == "host":
                client.host = parts[2]
                print(_c(f"  Host changed to: {client.host}", YELLOW))
                print(_c(f"  New URL: {client.base_url}", DIM))
            elif len(parts) == 3 and parts[1].lower() == "port":
                try:
                    client.port = int(parts[2])
                    print(_c(f"  Port changed to: {client.port}", YELLOW))
                    print(_c(f"  New URL: {client.base_url}", DIM))
                except ValueError:
                    print(_c(f"  Invalid port: {parts[2]!r}", RED))
            else:
                print(_c("  Usage: /config  |  /config host <host>  |  /config port <port>", DIM))
            continue

        # ── send to director ───────────────────────────────────────────────────
        print(SEP)
        print(_c("  Director is thinking…", DIM))
        print()
        try:
            client.chat(sid, user_input)
        except httpx.HTTPStatusError as exc:
            print(_c(f"[http error] {exc.response.status_code}: {exc.response.text}", RED))
        except Exception as exc:
            print(_c(f"[error] {exc}", RED))
        print()

    print()
    print(_c("Session ended.", DIM))
    print(_c(f"  Session ID: {sid}", DIM))
    print(_c(f"  Resume with: uv run python director_demo.py --session {sid}", DIM))
    print()

    client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Director super-agent interactive demo")
    # Separate host / port args (recommended)
    parser.add_argument("--host", default="localhost", help="Wiki server host (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Wiki server port (default: 8000)")
    # Legacy full-URL flag — takes precedence if provided
    parser.add_argument("--wiki-url", default=None, help="Override --host/--port with a full URL")
    parser.add_argument("--session", default=None, help="Resume an existing director session ID")
    args = parser.parse_args()

    if args.wiki_url:
        # Parse host/port out of the legacy --wiki-url flag
        url = args.wiki_url.rstrip("/")
        # Strip scheme
        without_scheme = url.split("://", 1)[-1]
        if ":" in without_scheme:
            host_part, port_part = without_scheme.rsplit(":", 1)
            host = host_part
            try:
                port = int(port_part)
            except ValueError:
                host = without_scheme
                port = 8000
        else:
            host = without_scheme
            port = 8000
    else:
        host = args.host
        port = args.port

    run_demo(host, port, args.session)


if __name__ == "__main__":
    main()
