"""Per-role model configuration."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Model strings and reliability settings sourced from env."""

    model_config = SettingsConfigDict(
        env_prefix="DEXTER_LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    planner: str = Field(default="replicate/anthropic/claude-4.5-sonnet")
    planner_fallback: str = Field(default="replicate/anthropic/claude-4.5-haiku")

    extractor: str = Field(default="replicate/anthropic/claude-4.5-sonnet")
    extractor_fallback: str = Field(default="replicate/anthropic/claude-4.5-haiku")

    judge: str = Field(default="replicate/anthropic/claude-4.5-haiku")
    judge_fallback: str = Field(default="replicate/anthropic/claude-4.5-sonnet")

    request_timeout_s: float = Field(default=20.0)
    num_retries: int = Field(default=3)

    breaker_failure_threshold: int = Field(default=3)
    breaker_window_seconds: int = Field(default=60)
    breaker_cooldown_seconds: int = Field(default=300)

    default_temperature: float = Field(default=0.0)
    default_max_tokens: int = Field(default=1024)


class LangfuseSettings(BaseSettings):
    """LangFuse callback credentials."""

    model_config = SettingsConfigDict(env_prefix="LANGFUSE_", env_file=".env", extra="ignore")
    public_key: str = Field(default="")
    secret_key: str = Field(default="")
    host: str = Field(default="https://cloud.langfuse.com")

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self.secret_key)


Role = str


def models_for(role: Role, settings: LLMSettings) -> list[str]:
    """Return [primary, fallback] for a role."""
    table: dict[str, tuple[str, str]] = {
        "planner": (settings.planner, settings.planner_fallback),
        "extractor": (settings.extractor, settings.extractor_fallback),
        "judge": (settings.judge, settings.judge_fallback),
    }
    if role not in table:
        raise ValueError(f"unknown LLM role {role!r}; expected one of {list(table)}")
    primary, fallback = table[role]
    return [primary, fallback]


llm_settings = LLMSettings()
langfuse_settings = LangfuseSettings()
