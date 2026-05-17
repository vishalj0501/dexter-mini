"""Provider-agnostic LLM client.

Everything that talks to a model goes through `LLMClient.complete(...)`. The
concrete implementation today is `LiteLLMClient`, which lets us hit any
LiteLLM-supported provider — Replicate by default for this account, but
swap the model string and we're on OpenAI / Anthropic / OpenRouter / vLLM /
anything else LiteLLM understands. That is the "not tied to one specific
provider" point from SPEC §6b, made literal.

Three things this client guarantees no matter the underlying provider:
  1. A normalized `Completion` shape — text, tool calls, usage, cost,
     latency, model that actually answered (post-fallback).
  2. A row in `audit_log` per invocation, indexed by `request_id`, so the
     LLM side of a trajectory joins to the tool side in SQL.
  3. Reliability knobs (retries, timeout, fallback) applied uniformly.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol
from uuid import UUID  # noqa: F401  (used implicitly via type hints)

import litellm
from litellm import acompletion
from litellm.exceptions import APIError
from pydantic import BaseModel, Field

from app.llm._settings import LLMSettings, Role, llm_settings, models_for
from app.llm.reliability import CircuitBreaker, default_breaker
from app.models import AuditLog
from app.schemas.enums import AuditAction

log = logging.getLogger(__name__)


litellm.suppress_debug_info = True


class Message(BaseModel):
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Completion(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    parsed: Any | None = None  # set when response_model was used
    model: str  # the model that actually answered (post-fallback)
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    latency_ms: int = 0
    role: str  # planner | extractor | judge


class LLMClient(Protocol):
    role: Role

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        request_id: str,
        actor: str = "agent",
        response_model: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion: ...


class LiteLLMClient:
    """LiteLLM-backed `LLMClient`. One per role; configured by the router."""

    def __init__(
        self,
        role: Role,
        primary: str,
        fallback: str,
        *,
        settings: LLMSettings | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.role = role
        self.primary = primary
        self.fallback = fallback
        self.settings = settings or llm_settings
        self.breaker = breaker or default_breaker

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        request_id: str,
        actor: str = "agent",
        response_model: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        if response_model is not None and tools:
            raise ValueError("complete(): pass either response_model or tools, not both")

        model_list = self._effective_model_list()
        start = time.perf_counter()
        payload: dict[str, Any] = {
            "role": self.role,
            "model_list": model_list,
            "has_tools": bool(tools),
            "structured": response_model.__name__ if response_model else None,
        }

        try:
            if response_model is not None:
                completion = await self._structured_call(
                    messages, model_list, response_model,
                    temperature=temperature, max_tokens=max_tokens,
                )
            else:
                completion = await self._chat_call(
                    messages, model_list, tools=tools,
                    temperature=temperature, max_tokens=max_tokens,
                )
        except Exception as exc:
            self.breaker.record_failure(model_list[0])
            payload["status"] = "error"
            payload["error"] = f"{type(exc).__name__}: {exc}"
            await self._audit(request_id, actor, payload, started=start)
            raise

        # Success on the model that actually answered (may be the fallback).
        self.breaker.record_success(completion.model)
        completion.latency_ms = int((time.perf_counter() - start) * 1000)
        completion.role = self.role
        payload.update({
            "status": "ok",
            "model_used": completion.model,
            "tokens": completion.usage.model_dump(),
            "cost_usd": completion.cost_usd,
            "latency_ms": completion.latency_ms,
            "tool_calls": [tc.name for tc in completion.tool_calls],
        })
        await self._audit(request_id, actor, payload, started=start)
        return completion

    def _effective_model_list(self) -> list[str]:
        """Apply the circuit breaker to the primary→fallback chain."""
        if self.breaker.is_open(self.primary):
            log.warning("llm: breaker open for %s — using fallback %s directly", self.primary, self.fallback)
            return [self.fallback]
        return [self.primary, self.fallback]

    async def _chat_call(
        self,
        messages: list[dict[str, Any]],
        model_list: list[str],
        *,
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> Completion:
        primary = model_list[0]
        fallbacks = model_list[1:]
        response = await acompletion(
            model=primary,
            messages=messages,
            fallbacks=fallbacks or None,
            num_retries=self.settings.num_retries,
            timeout=self.settings.request_timeout_s,
            temperature=temperature if temperature is not None else self.settings.default_temperature,
            max_tokens=max_tokens if max_tokens is not None else self.settings.default_max_tokens,
            tools=tools,
        )
        return _to_completion(response, role=self.role)

    async def _structured_call(
        self,
        messages: list[dict[str, Any]],
        model_list: list[str],
        response_model: type[BaseModel],
        *,
        temperature: float | None,
        max_tokens: int | None,
    ) -> Completion:
        """Structured output via Instructor — parse + retry-on-validation-failure."""
        import instructor  # local import keeps cold-start cheap when unused

        client = instructor.from_litellm(acompletion)
        primary = model_list[0]
        fallbacks = model_list[1:]
        parsed, raw = await client.chat.completions.create_with_completion(
            model=primary,
            messages=messages,
            response_model=response_model,
            max_retries=2,
            fallbacks=fallbacks or None,
            num_retries=self.settings.num_retries,
            timeout=self.settings.request_timeout_s,
            temperature=temperature if temperature is not None else self.settings.default_temperature,
            max_tokens=max_tokens if max_tokens is not None else self.settings.default_max_tokens,
        )
        completion = _to_completion(raw, role=self.role)
        completion.parsed = parsed
        return completion

    async def _audit(
        self,
        request_id: str,
        actor: str,
        payload: dict[str, Any],
        *,
        started: float,
    ) -> None:
        try:
            await AuditLog.create(
                request_id=request_id,
                action=AuditAction.LLM_CALL,
                actor=actor,
                payload=payload,
                latency_ms=int((time.perf_counter() - started) * 1000),
                cost_usd=payload.get("cost_usd"),
            )
        except Exception:
            log.exception("llm: failed to write audit row")

    
def _to_completion(response: Any, *, role: str) -> Completion:
    """Normalize LiteLLM's OpenAI-shaped response into our `Completion`."""
    choice = response.choices[0]
    message = choice.message

    tool_calls: list[ToolCall] = []
    raw_tool_calls = getattr(message, "tool_calls", None) or []
    for tc in raw_tool_calls:
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        args_raw = getattr(fn, "arguments", "") or ""
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except (json.JSONDecodeError, TypeError):
            args = {"_raw": args_raw}
        tool_calls.append(ToolCall(id=getattr(tc, "id", ""), name=fn.name, arguments=args))

    usage_obj = getattr(response, "usage", None)
    usage = Usage(
        prompt_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
        total_tokens=getattr(usage_obj, "total_tokens", 0) or 0,
    )

    cost = None
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict):
        cost = hidden.get("response_cost")

    return Completion(
        content=getattr(message, "content", None),
        tool_calls=tool_calls,
        model=getattr(response, "model", "unknown"),
        usage=usage,
        cost_usd=cost,
        role=role,
    )


__all__ = [
    "LLMClient",
    "LiteLLMClient",
    "Completion",
    "Message",
    "ToolCall",
    "Usage",
    "APIError",
]
