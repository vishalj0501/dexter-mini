"""Request-ID middleware.

Reads `X-Request-Id` from the inbound request (or generates a fresh UUID),
stamps it onto `request.state.request_id`, and echoes it back to the client
in the same header. Routes pull it via `request.state.request_id` and
forward it into the agent + tools — that single id then threads through the
audit_log, the LLM call rows, and (later) LangFuse trace ids.

One ID, three systems, queryable across all of them — see SPEC §6c.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("dexter-mini.obs")

HEADER = "X-Request-Id"


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get(HEADER)
        request_id = incoming or f"req-{uuid4().hex[:12]}"
        request.state.request_id = request_id
        log.debug("request %s %s rid=%s", request.method, request.url.path, request_id)
        response = await call_next(request)
        response.headers[HEADER] = request_id
        return response


def get_request_id(request: Request) -> str:
    """Helper for route handlers: fetch the middleware-stamped id."""
    rid = getattr(request.state, "request_id", None)
    if not rid:
        # Middleware not installed — degrade to a fresh id so the request
        # is still observable (just not joinable to anything upstream).
        rid = f"req-{uuid4().hex[:12]}"
        request.state.request_id = rid
    return rid


__all__ = ["RequestIDMiddleware", "get_request_id", "HEADER"]
