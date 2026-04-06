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

    # ── Agent identity IDs ────────────────────────────────────────────────────
    research_agent_id: str = "wiki-research"
    proposer_agent_id: str = "wiki-proposer"
    reviewer_agent_id: str = "wiki-reviewer"
    executor_agent_id: str = "wiki-executor"
    relation_agent_id: str = "wiki-relation"
    orchestrator_agent_id: str = "wiki-orchestrator"

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


settings = Settings()
