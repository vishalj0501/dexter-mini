"""Agent endpoint.

`POST /agent/run` accepts a transcript and runs the shift-copilot agent
against it. Returns the final agent state plus the request_id so the caller
can join into the audit log and trace store.

Streaming (SSE) lands in Day 5 when the frontend renders the live thinking;
for Day 3, this is a synchronous "run to completion and return" endpoint
that's enough to drive the smoke test and any curl-based exploration.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

from app.agent.graph import get_default_graph
from app.obs.middleware import get_request_id

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


class AgentRunRequest(BaseModel):
    transcript: str = Field(..., min_length=1, description="Caregiver utterance, already transcribed.")
    actor: str = Field("agent", description="Who is making the request — caregiver id / 'agent'.")
    recursion_limit: int = Field(30, ge=1, le=100, description="Hard cap on plan/act iterations.")


class AgentTrace(BaseModel):
    request_id: str
    final_message: str | None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/run", response_model=AgentTrace)
async def run_agent(req: AgentRunRequest, request: Request) -> AgentTrace:
    request_id = get_request_id(request)
    graph = get_default_graph()

    config = {
        "configurable": {
            "request_id": request_id,
            "actor": req.actor,
        },
        "recursion_limit": req.recursion_limit,
    }

    initial = {"messages": [HumanMessage(req.transcript)]}
    state = await graph.ainvoke(initial, config=config)
    return _to_trace(state, request_id)


def _to_trace(state: dict[str, Any], request_id: str) -> AgentTrace:
    messages = state.get("messages", [])
    final = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage) and m.content),
        None,
    )
    tool_calls: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            for tc in m.tool_calls:
                tool_calls.append({"name": tc["name"], "args": tc.get("args", {})})
    return AgentTrace(
        request_id=request_id,
        final_message=final.content if final else None,
        tool_calls=tool_calls,
        messages=[_serialise(m) for m in messages],
    )


def _serialise(message: Any) -> dict[str, Any]:
    base = {"type": message.__class__.__name__, "content": getattr(message, "content", None)}
    if isinstance(message, AIMessage) and message.tool_calls:
        base["tool_calls"] = [
            {"name": tc["name"], "args": tc.get("args", {})} for tc in message.tool_calls
        ]
    if isinstance(message, ToolMessage):
        base["name"] = message.name
        base["tool_call_id"] = message.tool_call_id
    return base
