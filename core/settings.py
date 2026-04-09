from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./wolfina.db"
    debug: bool = False
    # Minimum number of distinct reviewers required before a proposal can be approved.
    min_reviewers: int = 1

    # ── LLM Provider ─────────────────────────────────────────────────────────
    # "ollama" uses the Ollama Python SDK.
    # "openai_compat" uses httpx to call any OpenAI-format API (e.g. OpenRouter).
    llm_provider: str = "ollama"

    # Ollama settings
    ollama_host: str = "http://localhost:11434"
    ollama_api_key: str = ""

    # OpenAI-compat settings (OpenRouter, LM Studio, etc.)
    openai_compat_base_url: str = "https://openrouter.ai/api/v1"
    openai_compat_api_key: str = ""

    # Default model (used when a per-agent model is not set)
    default_model: str = "llama3.2"

    # ── Per-agent model overrides (empty string = use default_model) ─────────
    research_agent_model: str = ""
    proposer_agent_model: str = ""
    reviewer_agent_model: str = ""
    executor_agent_model: str = ""
    relation_agent_model: str = ""
    orchestrator_agent_model: str = ""
    ingest_agent_model: str = ""

    # ── Agent identity IDs ────────────────────────────────────────────────────
    research_agent_id: str = "wiki-research"
    proposer_agent_id: str = "wiki-proposer"
    reviewer_agent_id: str = "wiki-reviewer"
    executor_agent_id: str = "wiki-executor"
    relation_agent_id: str = "wiki-relation"
    orchestrator_agent_id: str = "wiki-orchestrator"
    ingest_agent_id: str = "wiki-ingest"
    director_agent_id: str = "wiki-director"

    # ── Per-agent model overrides (continued) ─────────────────────────────────
    director_agent_model: str = ""
    quick_query_agent_model: str = ""

    # ── Conversation flush thresholds ─────────────────────────────────────────
    flush_max_messages: int = 50         # flush after N messages
    flush_max_chars: int = 10000         # flush after N total characters
    flush_max_seconds: int = 300         # flush after N seconds since first message

    # ── Dynamic scheduler ─────────────────────────────────────────────────────
    scheduler_min_interval_seconds: int = 300    # 5 min  (high data rate)
    scheduler_max_interval_seconds: int = 3600   # 1 hour (idle)
    scheduler_rate_window_hours: int = 24        # look-back window for flush rate
    scheduler_target_rate: int = 10              # flushes/window that map to min_interval

    # Max LLM tool-loop iterations per agent run (safety limit)
    agent_max_iterations: int = 20

    # ── Task Janitor ──────────────────────────────────────────────────────────
    # How often the janitor patrol runs (independent of the maintenance pipeline).
    janitor_interval_seconds: int = 120          # 2 min
    # Tasks stuck in `running` longer than this are assumed crashed and re-queued.
    janitor_running_timeout_minutes: int = 5
    # Pending tasks older than this with no active maintenance run → nudge the scheduler.
    janitor_pending_timeout_minutes: int = 10
    # Max times the janitor will re-queue a failed task before giving up.
    janitor_max_task_retries: int = 3
    # Done/failed tasks older than this are deleted to prevent DB bloat.
    janitor_task_retention_days: int = 7

    # ── Ollama chat_template_kwargs ───────────────────────────────────────────
    # Set to true to pass {"thinking": False} in extra_body so models that
    # support extended thinking (e.g. Qwen3, Kimi-K2) skip the thinking phase.
    ollama_disable_thinking: bool = False

    # ── File read tool ────────────────────────────────────────────────────────
    # Comma-separated list of directories the LLM is allowed to read from.
    # Relative paths are resolved relative to the process working directory.
    # Empty string = feature disabled (no file reads allowed).
    # Example: FILE_READ_ALLOWED_DIRS=./docs,./data,/etc/myapp/config
    file_read_allowed_dirs: str = ""

    # Maximum file size (bytes) the read_file tool will return (default 128 KB).
    file_read_max_bytes: int = 131072


settings = Settings()
