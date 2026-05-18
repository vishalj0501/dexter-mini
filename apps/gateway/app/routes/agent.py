"""Agent endpoints — Day 4 (interrupt + resume).

`POST /agent/run` accepts a transcript and runs the agent until it either
finishes (status="complete") OR hits an `ask_caregiver` interrupt
(status="awaiting_caregiver"). The response carries a `thread_id` that the
client uses to resume.

`POST /agent/resume` accepts the user's reply for an outstanding question
and continues the agent from where it paused. Returns the same shape.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.agent.graph import get_default_graph
from app.obs.middleware import get_request_id

log = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


# ────────────────────────────────────────────────────────────────────────────
# Request / response models
# ────────────────────────────────────────────────────────────────────────────


class AgentRunRequest(BaseModel):
    transcript: str = Field(..., min_length=1, description="Caregiver utterance, already transcribed.")
    actor: str = Field("agent", description="Who is making the request — caregiver id / 'agent'.")
    thread_id: str | None = Field(
        None,
        description="Optional thread id. Omit to start a new conversation; the "
                    "response carries the assigned thread_id either way.",
    )
    recursion_limit: int = Field(30, ge=1, le=100)


class AgentResumeRequest(BaseModel):
    thread_id: str
    reply: str = Field(..., min_length=1, description="The caregiver's response to the agent's question.")
    actor: str = Field("agent")
    recursion_limit: int = Field(30, ge=1, le=100)


class PendingQuestion(BaseModel):
    question: str
    context: dict[str, Any] = Field(default_factory=dict)


class AgentTrace(BaseModel):
    status: Literal["complete", "awaiting_caregiver"]
    request_id: str
    thread_id: str
    final_message: str | None = None
    awaiting: PendingQuestion | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/run", response_model=AgentTrace)
async def run_agent(req: AgentRunRequest, request: Request) -> AgentTrace:
    request_id = get_request_id(request)
    thread_id = req.thread_id or f"thread-{uuid4().hex[:12]}"

    config = _build_config(thread_id=thread_id, request_id=request_id, actor=req.actor, recursion_limit=req.recursion_limit)
    graph = get_default_graph()
    state = await graph.ainvoke({"messages": [HumanMessage(req.transcript)]}, config=config)
    return _to_trace(state, request_id=request_id, thread_id=thread_id)


@router.post("/resume", response_model=AgentTrace)
async def resume_agent(req: AgentResumeRequest, request: Request) -> AgentTrace:
    request_id = get_request_id(request)
    config = _build_config(thread_id=req.thread_id, request_id=request_id, actor=req.actor, recursion_limit=req.recursion_limit)
    graph = get_default_graph()

    # Validate the thread exists and is genuinely paused on an interrupt.
    snapshot = await graph.aget_state(config)
    if snapshot is None or snapshot.created_at is None:
        raise HTTPException(status_code=404, detail=f"unknown thread_id {req.thread_id!r}")
    if not snapshot.tasks or not any(t.interrupts for t in snapshot.tasks):
        raise HTTPException(
            status_code=409,
            detail=f"thread {req.thread_id!r} is not awaiting a reply (no pending interrupt)",
        )

    state = await graph.ainvoke(Command(resume=req.reply), config=config)
    return _to_trace(state, request_id=request_id, thread_id=req.thread_id)



def _build_config(*, thread_id: str, request_id: str, actor: str, recursion_limit: int) -> dict[str, Any]:
    return {
        "configurable": {
            "thread_id": thread_id,
            "request_id": request_id,
            "actor": actor,
        },
        "recursion_limit": recursion_limit,
    }


def _to_trace(state: dict[str, Any], *, request_id: str, thread_id: str) -> AgentTrace:
    # Interrupts surface in the state dict under the "__interrupt__" key.
    interrupts = state.get("__interrupt__") or []
    if interrupts:
        # We only ever raise one interrupt at a time (ask_caregiver).
        i = interrupts[0]
        # `value` is whatever we passed to interrupt(); shape: {question, context}
        val = getattr(i, "value", None) or {}
        return AgentTrace(
            status="awaiting_caregiver",
            request_id=request_id,
            thread_id=thread_id,
            awaiting=PendingQuestion(
                question=val.get("question", ""),
                context=val.get("context") or {},
            ),
            tool_calls=_extract_tool_calls(state.get("messages", [])),
            messages=[_serialise(m) for m in state.get("messages", [])],
        )

    messages = state.get("messages", [])
    final = state.get("final_answer") or _last_ai_content(messages)
    return AgentTrace(
        status="complete",
        request_id=request_id,
        thread_id=thread_id,
        final_message=final,
        tool_calls=_extract_tool_calls(messages),
        messages=[_serialise(m) for m in messages],
    )


def _extract_tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """Walk message history and pull out the parsed actions.

    For Day 4 we re-parse the AIMessage content (ReAct text) rather than
    rely on .tool_calls — Replicate doesn't emit those.
    """
    from app.agent.react_parser import AgentAction, parse_react_output

    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, AIMessage) or not m.content:
            continue
        try:
            parsed = parse_react_output(m.content)
        except Exception:
            continue
        if isinstance(parsed, AgentAction):
            out.append({"name": parsed.tool, "args": parsed.tool_input})
    return out


def _last_ai_content(messages: list[Any]) -> str | None:
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m.content
    return None


def _serialise(message: Any) -> dict[str, Any]:
    base = {"type": message.__class__.__name__, "content": getattr(message, "content", None)}
    if isinstance(message, ToolMessage):
        base["name"] = message.name
        base["tool_call_id"] = message.tool_call_id
    return base
