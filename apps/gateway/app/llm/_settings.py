"""Per-role model configuration.

Every call site declares a `role` — planner, extractor, or judge. The router
maps that role to a primary + fallback model. Both are LiteLLM model strings;
the default prefix is `replicate/...` because that's what this account has
keys for, but any LiteLLM-supported provider works (openai/, anthropic/,
openrouter/, etc.) — change the env var and nothing else.

This is the single source of truth for the model cascade described in SPEC §8.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Role → (primary, fallback) model strings, sourced from env."""

    model_config = SettingsConfigDict(
        env_prefix="DEXTER_LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Claude-only by design — the demo is built and tuned on Claude's ReAct
    # format. The cascade stays in-family: Sonnet → Haiku for heavy roles,
    # Haiku → Sonnet for the judge.

    # Planner — picks the next tool. Wants reasoning quality; falls back to
    # Haiku (faster, less prone to Replicate cold-start).
    planner: str = Field(default="replicate/anthropic/claude-4.5-sonnet")
    planner_fallback: str = Field(default="replicate/anthropic/claude-4.5-haiku")

    # Extractor — fills the SIS Pydantic schemas. Wants the strongest model.
    extractor: str = Field(default="replicate/anthropic/claude-4.5-sonnet")
    extractor_fallback: str = Field(default="replicate/anthropic/claude-4.5-haiku")

    # Judge — grounds drafts against the transcript. Cheap + fast > big.
    judge: str = Field(default="replicate/anthropic/claude-4.5-haiku")
    judge_fallback: str = Field(default="replicate/anthropic/claude-4.5-sonnet")

    # Reliability knobs (apply uniformly across roles)
    request_timeout_s: float = Field(default=20.0)
    num_retries: int = Field(default=3)

    # Circuit-breaker tuning
    breaker_failure_threshold: int = Field(default=3)
    breaker_window_seconds: int = Field(default=60)
    breaker_cooldown_seconds: int = Field(default=300)

    # Deterministic by default — overridable per call.
    default_temperature: float = Field(default=0.0)
    default_max_tokens: int = Field(default=1024)


class LangfuseSettings(BaseSettings):
    """LangFuse credentials — picked up by LiteLLM's success callback.

    Source of truth for whether LangFuse is enabled is the *presence* of
    LANGFUSE_PUBLIC_KEY in env. If unset, the callback is skipped silently.
    """

    model_config = SettingsConfigDict(env_prefix="LANGFUSE_", env_file=".env", extra="ignore")
    public_key: str = Field(default="")
    secret_key: str = Field(default="")
    host: str = Field(default="https://cloud.langfuse.com")

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self.secret_key)


Role = str  # one of {"planner", "extractor", "judge"}


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
