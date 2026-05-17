"""Per-model circuit breaker.

LiteLLM already handles per-call retries, fallback, and timeout. This layer
sits one step above: if a model has been failing recently, keep it *out* of
the rotation entirely for a cooldown period so we don't burn latency every
request waiting for retries on a known-sick provider.

Semantics:
  - Closed (normal): all calls go primary → fallback.
  - Open: primary is bypassed; the call goes straight to the fallback for the
    next `cooldown_seconds`. After cooldown, the breaker resets to closed and
    primary is tried again.

The breaker is in-memory and per-process — fine for one gateway instance,
which is the deploy target. A multi-instance setup would back it with Redis.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable

from app.llm._settings import llm_settings


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        window_seconds: int = 60,
        cooldown_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._opened_at: dict[str, float] = {}



    def is_open(self, model: str) -> bool:
        opened = self._opened_at.get(model)
        if opened is None:
            return False
        if (self._clock() - opened) >= self.cooldown_seconds:
            self._reset(model)
            return False
        return True

    def state(self, model: str) -> str:
        return "open" if self.is_open(model) else "closed"


    def record_failure(self, model: str) -> None:
        now = self._clock()
        self._prune(model, now)
        self._failures[model].append(now)
        if len(self._failures[model]) >= self.failure_threshold and model not in self._opened_at:
            self._opened_at[model] = now

    def record_success(self, model: str) -> None:
        self._reset(model)

    def _prune(self, model: str, now: float) -> None:
        cutoff = now - self.window_seconds
        bucket = self._failures[model]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _reset(self, model: str) -> None:
        self._failures.pop(model, None)
        self._opened_at.pop(model, None)


default_breaker = CircuitBreaker(
    failure_threshold=llm_settings.breaker_failure_threshold,
    window_seconds=llm_settings.breaker_window_seconds,
    cooldown_seconds=llm_settings.breaker_cooldown_seconds,
)


__all__ = ["CircuitBreaker", "default_breaker"]
