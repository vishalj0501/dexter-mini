"""Tests for app/llm/.

We monkeypatch `litellm.acompletion` so no real API calls happen. The shape we
mock matches LiteLLM's OpenAI-compatible response object.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app import llm as llm_pkg
from app.llm.client import LiteLLMClient, ToolCall
from app.llm.reliability import CircuitBreaker
from app.models import AuditLog
from tests.conftest import REQUEST_ID


# ---------- helpers ----------


def _mock_response(
    *,
    content: str | None = "ok",
    tool_calls: list[dict] | None = None,
    model: str = "replicate/anthropic/claude-sonnet-4-5",
    prompt_tokens: int = 12,
    completion_tokens: int = 7,
    cost: float | None = 0.0001,
) -> SimpleNamespace:
    tcs = []
    for tc in tool_calls or []:
        tcs.append(
            SimpleNamespace(
                id=tc.get("id", "call_1"),
                function=SimpleNamespace(
                    name=tc["name"],
                    arguments=json.dumps(tc.get("arguments", {})),
                ),
            )
        )
    message = SimpleNamespace(content=content, tool_calls=tcs or None)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model=model,
        _hidden_params={"response_cost": cost},
    )


def _make_client(primary="model-primary", fallback="model-fallback", breaker=None) -> LiteLLMClient:
    return LiteLLMClient(
        role="planner",
        primary=primary,
        fallback=fallback,
        breaker=breaker or CircuitBreaker(failure_threshold=2, window_seconds=60, cooldown_seconds=300),
    )


# ---------- plain chat ----------


async def test_complete_plain_chat_returns_normalized_completion(monkeypatch):
    mock = AsyncMock(return_value=_mock_response(content="Hello there."))
    monkeypatch.setattr("app.llm.client.acompletion", mock)

    client = _make_client()
    result = await client.complete(
        [{"role": "user", "content": "hi"}],
        request_id=REQUEST_ID,
    )
    assert result.content == "Hello there."
    assert result.role == "planner"
    assert result.usage.total_tokens == 19
    assert result.cost_usd == 0.0001
    assert result.latency_ms >= 0
    mock.assert_awaited_once()


async def test_complete_writes_audit_log(monkeypatch):
    mock = AsyncMock(return_value=_mock_response(content="hi"))
    monkeypatch.setattr("app.llm.client.acompletion", mock)

    client = _make_client()
    await client.complete([{"role": "user", "content": "?"}], request_id=REQUEST_ID)

    rows = await AuditLog.all()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "llm_call"
    assert row.request_id == REQUEST_ID
    assert row.payload["status"] == "ok"
    assert row.payload["model_used"]
    assert row.payload["tokens"]["total_tokens"] == 19
    assert row.cost_usd == 0.0001


# ---------- tool calling ----------


async def test_complete_parses_tool_calls(monkeypatch):
    response = _mock_response(
        content=None,
        tool_calls=[{"id": "call_1", "name": "get_resident", "arguments": {"name_or_id": "Müller"}}],
    )
    mock = AsyncMock(return_value=response)
    monkeypatch.setattr("app.llm.client.acompletion", mock)

    client = _make_client()
    result = await client.complete(
        [{"role": "user", "content": "find müller"}],
        tools=[{"type": "function", "function": {"name": "get_resident"}}],
        request_id=REQUEST_ID,
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0] == ToolCall(
        id="call_1", name="get_resident", arguments={"name_or_id": "Müller"}
    )


async def test_complete_handles_malformed_tool_arguments(monkeypatch):
    # LLM sometimes returns invalid JSON in tool args; we should not crash.
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="get_resident", arguments="{invalid json"),
            )],
        ))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="m",
        _hidden_params={},
    )
    monkeypatch.setattr("app.llm.client.acompletion", AsyncMock(return_value=response))

    client = _make_client()
    result = await client.complete([{"role": "user", "content": "?"}], request_id=REQUEST_ID)
    assert result.tool_calls[0].arguments == {"_raw": "{invalid json"}


# ---------- failure paths ----------


async def test_complete_propagates_error_and_audits_it(monkeypatch):
    mock = AsyncMock(side_effect=RuntimeError("provider down"))
    monkeypatch.setattr("app.llm.client.acompletion", mock)

    client = _make_client()
    with pytest.raises(RuntimeError):
        await client.complete([{"role": "user", "content": "?"}], request_id=REQUEST_ID)

    rows = await AuditLog.all()
    assert len(rows) == 1
    assert rows[0].payload["status"] == "error"
    assert "provider down" in rows[0].payload["error"]


async def test_complete_rejects_both_response_model_and_tools(monkeypatch):
    class Foo(BaseModel):
        x: int

    client = _make_client()
    with pytest.raises(ValueError):
        await client.complete(
            [],
            response_model=Foo,
            tools=[{"type": "function", "function": {"name": "x"}}],
            request_id=REQUEST_ID,
        )


# ---------- circuit breaker ----------


async def test_breaker_opens_after_threshold_failures_and_routes_to_fallback(monkeypatch):
    breaker = CircuitBreaker(failure_threshold=2, window_seconds=60, cooldown_seconds=300)
    client = _make_client(primary="p", fallback="f", breaker=breaker)

    # Trip the breaker for the primary
    breaker.record_failure("p")
    breaker.record_failure("p")
    assert breaker.is_open("p") is True

    captured: dict = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return _mock_response(content="ok", model="f")

    monkeypatch.setattr("app.llm.client.acompletion", fake)

    result = await client.complete([{"role": "user", "content": "?"}], request_id=REQUEST_ID)
    # With breaker open on primary, the call goes straight to fallback as the new primary
    # and there are no further fallbacks.
    assert captured["model"] == "f"
    assert captured["fallbacks"] is None
    assert result.model == "f"


async def test_breaker_success_clears_failure_state(monkeypatch):
    breaker = CircuitBreaker(failure_threshold=2, window_seconds=60, cooldown_seconds=300)
    breaker.record_failure("p")
    breaker.record_failure("p")
    assert breaker.is_open("p")
    breaker.record_success("p")
    assert breaker.is_open("p") is False


def test_breaker_cooldown_resets_after_window():
    # Inject a tickable clock so we don't actually sleep.
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, window_seconds=60, cooldown_seconds=300, clock=lambda: now[0])
    breaker.record_failure("p")
    breaker.record_failure("p")
    assert breaker.is_open("p")
    now[0] += 301  # past cooldown
    assert breaker.is_open("p") is False


# ---------- router ----------


def test_router_returns_role_specific_client(monkeypatch):
    monkeypatch.setenv("DEXTER_LLM_PLANNER", "test/primary-planner")
    monkeypatch.setenv("DEXTER_LLM_PLANNER_FALLBACK", "test/fallback-planner")
    # Settings cache is module-level; force a reload of the settings + router.
    from app.llm import _settings as s_mod
    s_mod.llm_settings = s_mod.LLMSettings()
    from app.llm import router as r_mod
    r_mod.clear_cache()

    client = r_mod.get_client("planner")
    assert client.role == "planner"
    assert client.primary == "test/primary-planner"
    assert client.fallback == "test/fallback-planner"


def test_router_rejects_unknown_role():
    with pytest.raises(ValueError):
        llm_pkg.get_client("nonexistent")
