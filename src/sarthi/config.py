"""Runtime settings, loaded from environment variables and an optional ``.env`` file.

Secrets are typed as ``SecretStr`` so they never appear in ``repr()``, logs or tracebacks.
Read a secret's value only at the point of use with ``.get_secret_value()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All Sarthi settings. Environment variables use the ``SARTHI_`` prefix."""

    model_config = SettingsConfigDict(
        env_prefix="SARTHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("data"), description="User data: database, resume, keys.")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    telegram_bot_token: SecretStr | None = None
    digest_hour: int = Field(default=19, ge=0, le=23, description="Local hour for the daily digest.")

    # Job sources. Company boards are "<hiring system>/<company slug>", e.g. "greenhouse/razorpay".
    job_boards: list[str] = Field(default=["linkedin", "indeed"], description="JobSpy boards to search.")
    watch_companies: list[str] = Field(default=[], description="Company boards to watch.")
    boards_every_minutes: int = Field(default=60, ge=15)
    companies_every_minutes: int = Field(default=30, ge=5)
    feeds: list[str] = Field(
        default=[
            "remotive",
            "remoteok",
            "weworkremotely",
            "himalayas",
            "jobicy",
            "arbeitnow",
            "workingnomads",
            "hackernews",
        ],
        description="Free public job feeds (no key).",
    )
    feeds_every_minutes: int = Field(default=60, ge=15)
    freehire: bool = Field(default=True, description="Search Freehire (~90 hiring systems) for your roles.")
    pages_every_minutes: int = Field(default=60, ge=15, description="Pages added with /watch.")

    # Telegram job channels, read with your own account (read-only): get an API id and hash at
    # https://my.telegram.org, then run `uv run sarthi login telegram` once.
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    telegram_channels: list[str] = Field(default=[], description="Channel usernames, e.g. @jobs_channel.")
    channels_every_minutes: int = Field(default=30, ge=10)

    @property
    def channels_enabled(self) -> bool:
        """True when channels are listed and the Telegram API id and hash are set."""
        return bool(self.telegram_channels and self.telegram_api_id and self.telegram_api_hash)

    # Applying: forms are filled in a browser without a window; nothing is submitted without your approval.
    apply_headless: bool = True
    apply_daily_limit: int = Field(default=30, ge=1, le=500, description="Applications per site per day.")

    # Reply tracking: Sarthi reads (never changes) your mailbox over IMAP.
    # Gmail: imap.gmail.com and an app password.
    mail_imap_host: str = ""
    mail_imap_port: int = Field(default=993, ge=1, le=65535)
    mail_user: str = ""
    mail_password: SecretStr | None = None
    mail_folder: str = "INBOX"
    mail_every_minutes: int = Field(default=10, ge=2, le=1440)
    followup_after_days: int = Field(default=10, ge=3, le=60)

    @property
    def mail_enabled(self) -> bool:
        """True when a mailbox is set up for reply tracking."""
        return bool(self.mail_imap_host and self.mail_user and self.mail_password)

    # AI models, tried in order until one answers: "<provider>/<model>", e.g. "gemini/gemini-2.5-flash".
    # Providers: ollama, gemini, openai, anthropic, groq, openrouter, deepseek (see sarthi.ai.providers).
    llm_fast_models: list[str] = Field(default=["ollama/llama3.1"], description="For matching.")
    llm_quality_models: list[str] = Field(default=["ollama/llama3.1"], description="For writing.")
    llm_daily_token_budget: int = Field(
        default=500_000, ge=0, description="Paid providers only. 0: no limit."
    )
    llm_requests_per_minute: int = Field(default=30, ge=1)
    ollama_url: str = "http://localhost:11434/v1"
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None

    @property
    def db_path(self) -> Path:
        """Path of the single SQLite database file."""
        return self.data_dir / "sarthi.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
