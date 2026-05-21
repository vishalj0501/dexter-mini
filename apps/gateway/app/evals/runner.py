"""Run a single eval case end-to-end and capture everything we'll score."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_agent_graph
from app.evals.cases import EvalCase
from app.llm import LLMClient
from app.models import AuditLog, CareEvent, ReviewFlag

log = logging.getLogger(__name__)


@dataclass
class CaseResult:
    """Everything we need to score one case run."""
    case_id: str
    request_id: str
    thread_id: str

    # Trajectory
    completed: bool
    error: str | None = None
    final_message: str | None = None
    tool_sequence: list[str] = field(default_factory=list)

    # Outcome (read fresh from DB after the run)
    drafted_entry_ids: list[UUID] = field(default_factory=list)
    drafted_themes: list[str] = field(default_factory=list)
    flag_ids: list[UUID] = field(default_factory=list)
    validation_passes: list[bool] = field(default_factory=list)

    # Latency / cost (from audit_log llm_call rows)
    llm_call_count: int = 0
    total_latency_ms: int = 0
    total_cost_usd: float = 0.0


async def run_case(
    case: EvalCase,
    *,
    client: LLMClient,
    recursion_limit: int = 50,
) -> CaseResult:
    """Run one case against a freshly-built graph.

    A fresh MemorySaver per case keeps thread state hermetic — interrupts
    or accumulated drafts from a prior case can't leak in.
    """
    request_id = f"eval-{case.id}-{uuid4().hex[:8]}"
    thread_id = f"eval-{case.id}-{uuid4().hex[:8]}"
    result = CaseResult(case_id=case.id, request_id=request_id, thread_id=thread_id, completed=False)

    graph = build_agent_graph(client=client, checkpointer=MemorySaver())
    config = {
        "configurable": {
            "thread_id": thread_id,
            "request_id": request_id,
            "actor": "eval",
        },
        "recursion_limit": recursion_limit,
    }

    t0 = time.perf_counter()
    try:
        state = await graph.ainvoke({"messages": [HumanMessage(content=case.transcript)]}, config=config)
        result.completed = True
        result.final_message = state.get("final_answer") or ""
    except Exception as exc:  # noqa: BLE001 — eval must record any failure
        result.error = f"{type(exc).__name__}: {exc}"
        log.warning("eval run %s errored: %s", case.id, result.error)
    dur_ms = int((time.perf_counter() - t0) * 1000)
    log.info("eval %s done in %dms (completed=%s)", case.id, dur_ms, result.completed)

    # Reconstruct the trajectory from audit + DB rows. We trust the DB over
    # the model's narration; that's the whole point.
    await _populate_from_audit(result)
    return result


async def _populate_from_audit(result: CaseResult) -> None:
    rows = await AuditLog.filter(request_id=result.request_id).order_by("created_at").all()
    for row in rows:
        action = row.action or ""
        if action.startswith("tool."):
            result.tool_sequence.append(action.removeprefix("tool."))
        if action == "llm_call":
            result.llm_call_count += 1
            result.total_latency_ms += int(row.latency_ms or 0)
            result.total_cost_usd += float(row.cost_usd or 0.0)
        if action == "tool.validate_entry":
            payload = row.payload or {}
            out = payload.get("result") or {}
            if isinstance(out, dict) and "passed" in out:
                result.validation_passes.append(bool(out["passed"]))

    drafts = await CareEvent.filter(request_id=result.request_id).all()
    result.drafted_entry_ids = [d.id for d in drafts]
    result.drafted_themes = [
        (d.theme.value if hasattr(d.theme, "value") else str(d.theme)) for d in drafts
    ]

    flags = await ReviewFlag.filter(request_id=result.request_id).all()
    result.flag_ids = [f.id for f in flags]
