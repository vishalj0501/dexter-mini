"""Provider-agnostic LLM layer.

Public surface:
  - `get_client(role)` → an `LLMClient` for "planner" | "extractor" | "judge"
  - `Completion`, `ToolCall`, `Usage` — normalized response shapes
  - `default_breaker` — the in-process circuit breaker (mostly used by tests)
"""

from app.llm.client import Completion, LLMClient, LiteLLMClient, Message, ToolCall, Usage
from app.llm.reliability import CircuitBreaker, default_breaker
from app.llm.router import clear_cache, get_client

__all__ = [
    "LLMClient",
    "LiteLLMClient",
    "Completion",
    "Message",
    "ToolCall",
    "Usage",
    "CircuitBreaker",
    "default_breaker",
    "get_client",
    "clear_cache",
]
