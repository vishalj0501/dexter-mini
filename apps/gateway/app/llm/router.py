"""Role-to-LLMClient lookup."""

from __future__ import annotations

from functools import lru_cache

from app.llm import _settings as _s
from app.llm._settings import Role, models_for
from app.llm.client import LiteLLMClient, LLMClient
from app.llm.reliability import default_breaker


@lru_cache(maxsize=8)
def get_client(role: Role) -> LLMClient:
    """Return a configured LLM client for the given role."""
    settings = _s.llm_settings
    primary, fallback = models_for(role, settings)
    return LiteLLMClient(
        role=role,
        primary=primary,
        fallback=fallback,
        settings=settings,
        breaker=default_breaker,
    )


def clear_cache() -> None:
    """Drop cached clients — handy after changing env in tests."""
    get_client.cache_clear()


__all__ = ["get_client", "clear_cache"]
