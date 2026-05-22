"""Tool-call auditing."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar
from uuid import UUID

from pydantic import BaseModel

from app.models import AuditLog
from app.schemas.enums import AuditAction

log = logging.getLogger(__name__)

T = TypeVar("T")

_MAX_STR = 500
_SYSTEM_KWARGS = {"request_id", "actor"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (UUID,)):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)[:_MAX_STR]


def _summarise_args(fn: Callable, args: tuple, kwargs: dict) -> dict:
    sig = inspect.signature(fn)
    try:
        bound = sig.bind(*args, **kwargs)
    except TypeError:
        return {"args": _json_safe(list(args)), "kwargs": _json_safe(kwargs)}
    bound.apply_defaults()
    return {
        name: _json_safe(value)
        for name, value in bound.arguments.items()
        if name not in _SYSTEM_KWARGS
    }


def audited(action: AuditAction) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Wrap an async tool so every invocation is recorded in audit_log."""

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            request_id = kwargs.get("request_id")
            if not request_id:
                raise ValueError(f"{fn.__name__}: request_id kwarg is required")
            actor = kwargs.get("actor", "agent")

            payload: dict[str, Any] = {"input": _summarise_args(fn, args, kwargs)}
            started = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
            except Exception as exc:
                payload["status"] = "error"
                payload["error"] = f"{type(exc).__name__}: {exc}"
                await _record(action, actor, request_id, payload, started)
                raise
            payload["status"] = "ok"
            payload["result"] = _json_safe(result)
            await _record(action, actor, request_id, payload, started)
            return result

        wrapper.__audit_action__ = action
        return wrapper

    return decorator


async def _record(
    action: AuditAction,
    actor: str,
    request_id: str,
    payload: dict,
    started: float,
) -> None:
    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        await AuditLog.create(
            request_id=request_id,
            action=action,
            actor=actor,
            payload=payload,
            latency_ms=latency_ms,
        )
    except Exception:
        log.exception("audit: failed to write audit row for %s", action)
