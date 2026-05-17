"""Role → LLMClient lookup.

Call sites declare a role; this router picks the configured primary + fallback
models and returns a ready-to-call client. Built per-process (clients are
stateless apart from the shared breaker), so this is just a small cache.
"""

from __future__ import annotations

from functools import lru_cache

from app.llm import _settings as _s
from app.llm._settings import Role, models_for
from app.llm.client import LiteLLMClient, LLMClient
from app.llm.reliability import default_breaker


@lru_cache(maxsize=8)
def get_client(role: Role) -> LLMClient:
    """Return a configured LLM client for the given role.

    Roles: "planner" | "extractor" | "judge". The mapping to model strings
    is in `app/llm/_settings.py` (env-overridable via `DEXTER_LLM_*`).
    """
    settings = _s.llm_settings  # read module attribute lazily
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
