"""Request-ID middleware."""

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
    """Fetch the middleware-stamped request id."""
    rid = getattr(request.state, "request_id", None)
    if not rid:
        rid = f"req-{uuid4().hex[:12]}"
        request.state.request_id = rid
    return rid


__all__ = ["RequestIDMiddleware", "get_request_id", "HEADER"]
