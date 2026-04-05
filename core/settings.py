from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./wolfina.db"
    debug: bool = False
    # Minimum number of distinct reviewers required before a proposal can be approved.
    min_reviewers: int = 1


settings = Settings()
