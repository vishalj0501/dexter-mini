"""Tests for the agent layer (prompted ReAct graph).

We use a `ScriptedClient` that returns a fixed sequence of LLM completions —
text responses in ReAct format — so the agent's trajectory is deterministic
and we never touch a real LLM provider.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import build_agent_graph
from app.agent.llm_tools import ALL_TOOLS, get_resident as get_resident_tool
from app.agent.react_parser import (
    AgentAction,
    AgentFinish,
    ParseError,
    parse_react_output,
)
from app.llm.client import Completion, Usage
from app.models import AuditLog, CareEvent
from app.schemas.enums import EventStatus, Theme
from tests.conftest import REQUEST_ID


# ────────────────────────────────────────────────────────────────────────────
# Parser
# ────────────────────────────────────────────────────────────────────────────


def test_parser_extracts_action():
    text = """\
Thought: I need to resolve the resident first.
Action: get_resident
Action Input: {"name_or_id": "Frau Müller"}
"""
    out = parse_react_output(text)
    assert isinstance(out, AgentAction)
    assert out.tool == "get_resident"
    assert out.tool_input == {"name_or_id": "Frau Müller"}
    assert out.thought is not None


def test_parser_extracts_final_answer():
    text = """\
Thought: I've documented everything.
Final Answer: Documented Frau Müller's vitals and breakfast for today.
"""
    out = parse_react_output(text)
    assert isinstance(out, AgentFinish)
    assert "Müller" in out.output


def test_parser_tolerates_markdown_bolding():
    """LLMs sometimes wrap keywords in **bold**. We strip those before matching."""
    text = """\
**Thought:** check vitals
**Action:** check_vital_ranges
**Action Input:** {"resident_id": "abc", "vitals": {"bp_systolic": 132}}
"""
    out = parse_react_output(text)
    assert isinstance(out, AgentAction)
    assert out.tool == "check_vital_ranges"
    assert out.tool_input["vitals"]["bp_systolic"] == 132


def test_parser_tolerates_code_fences_around_input():
    text = """\
Thought: drafting nutrition
Action: draft_sis_entry
Action Input:
```json
{"theme": "nutrition", "resident_id": "abc", "content": {"appetite": "good"}}
```
"""
    out = parse_react_output(text)
    assert isinstance(out, AgentAction)
    assert out.tool == "draft_sis_entry"
    assert out.tool_input["theme"] == "nutrition"


def test_parser_empty_input_object():
    text = "Thought: done querying\nAction: list_pending_documentation\nAction Input: {}"
    out = parse_react_output(text)
    assert isinstance(out, AgentAction)
    assert out.tool_input == {}


def test_parser_rejects_garbage():
    with pytest.raises(ParseError):
        parse_react_output("I think I should probably do something.")


def test_parser_first_marker_wins_when_both_present():
    """Generation order is truth — if Action comes first, the Final Answer
    that follows is a hallucination and we treat the response as an Action."""
    text = """\
Action: get_resident
Action Input: {"name_or_id": "x"}
Final Answer: Already done.
"""
    out = parse_react_output(text)
    assert isinstance(out, AgentAction)
    assert out.tool == "get_resident"


def test_parser_truncates_at_hallucinated_observation():
    """Model wrote its own fake Observation and a Final Answer afterwards.
    Both should be discarded; the Action before the Observation is what counts."""
    text = """\
Thought: I'll resolve the resident.
Action: get_resident
Action Input: {"name_or_id": "Müller"}
Observation: {"status": "resolved"}
Thought: now I'll wrap up.
Final Answer: Done.
"""
    out = parse_react_output(text)
    assert isinstance(out, AgentAction)
    assert out.tool == "get_resident"


# ────────────────────────────────────────────────────────────────────────────
# Tool wrapper unit tests
# ────────────────────────────────────────────────────────────────────────────


async def test_tool_wrapper_resolves_resident(resident):
    config = {"configurable": {"request_id": REQUEST_ID, "actor": "nurse-anna"}}
    result = await get_resident_tool.ainvoke({"name_or_id": "Müller"}, config=config)
    assert result["status"] == "resolved"
    assert result["resident"]["id"] == str(resident.id)


async def test_tool_wrapper_requires_request_id():
    with pytest.raises(ValueError, match="request_id"):
        await get_resident_tool.ainvoke({"name_or_id": "x"}, config={"configurable": {}})


async def test_tool_wrapper_writes_audit_row(resident):
    config = {"configurable": {"request_id": REQUEST_ID, "actor": "nurse-x"}}
    await get_resident_tool.ainvoke({"name_or_id": "Müller"}, config=config)
    rows = await AuditLog.all()
    assert any(r.request_id == REQUEST_ID and r.action == "tool.get_resident" for r in rows)
    assert any(r.actor == "nurse-x" for r in rows)


# ────────────────────────────────────────────────────────────────────────────
# Graph (with a scripted LLM client)
# ────────────────────────────────────────────────────────────────────────────


class ScriptedClient:
    """An LLMClient stand-in that yields a fixed sequence of text completions."""

    role = "planner"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._i = 0
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, *, request_id, actor="agent", **_) -> Completion:
        if self._i >= len(self._responses):
            raise RuntimeError("ScriptedClient ran out of responses")
        text = self._responses[self._i]
        self._i += 1
        self.calls.append({"request_id": request_id, "messages": messages})
        return Completion(
            content=text,
            model="scripted",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            role=self.role,
        )


async def test_graph_runs_get_resident_then_finishes(resident):
    """Two-step plan: resolve resident → declare done."""
    rid = str(resident.id)
    client = ScriptedClient([
        # Turn 1: call get_resident.
        f"""\
Thought: resolve the resident first.
Action: get_resident
Action Input: {{"name_or_id": "Müller"}}
""",
        # Turn 2: after observing the resolution, finish.
        """\
Thought: I have the id; nothing else asked.
Final Answer: Resolved Frau Müller (room 12). No documentation requested.
""",
    ])
    graph = build_agent_graph(client=client, tools=ALL_TOOLS)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("who is Müller in room 12?")]},
        config={"configurable": {"request_id": "graph-test-1", "actor": "tester"}},
    )

    assert state["done"] is True
    assert "Müller" in state["final_answer"]

    # Audit-log row for the tool call exists, joinable by request_id.
    rows = await AuditLog.filter(request_id="graph-test-1").all()
    actions = {r.action for r in rows}
    assert "tool.get_resident" in actions


async def test_graph_writes_draft_through_full_loop(resident):
    """Scripted: resolve → draft vitals → finalize wrap-up."""
    rid = str(resident.id)
    client = ScriptedClient([
        f"""\
Thought: resolve resident.
Action: get_resident
Action Input: {{"name_or_id": "Müller"}}
""",
        f"""\
Thought: draft her vitals.
Action: draft_sis_entry
Action Input: {{
    "theme": "vitals",
    "resident_id": "{rid}",
    "content": {{"bp_systolic": 130, "bp_diastolic": 82, "heart_rate": 72}},
    "source_transcript": "BP 130 over 82, pulse 72."
}}
""",
        """\
Thought: done.
Final Answer: Drafted vitals (BP 130/82, HR 72) for Frau Müller.
""",
    ])
    graph = build_agent_graph(client=client, tools=ALL_TOOLS)
    await graph.ainvoke(
        {"messages": [HumanMessage("BP 130/82, pulse 72 for Frau Müller.")]},
        config={"configurable": {"request_id": "graph-test-2", "actor": "tester"}},
    )

    drafts = await CareEvent.filter(request_id="graph-test-2").all()
    assert len(drafts) == 1
    assert drafts[0].theme == Theme.VITALS
    assert drafts[0].status == EventStatus.DRAFT
    assert drafts[0].content["bp_systolic"] == 130


async def test_graph_recovers_from_parse_error(resident):
    """If turn 1 is unparseable, the graph injects a repair prompt and loops.
    Turn 2 should produce a valid Final Answer and finish."""
    client = ScriptedClient([
        "I'll just chat about this for a bit.",  # No Action, no Final Answer
        "Thought: ok.\nFinal Answer: Recovered.",
    ])
    graph = build_agent_graph(client=client, tools=ALL_TOOLS)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("?")]},
        config={"configurable": {"request_id": "graph-test-3", "actor": "tester"}},
        # Allow extra recursion budget for the repair loop.
        # (default in langgraph is 25, plenty here.)
    )
    assert state["done"] is True
    assert "Recovered" in state["final_answer"]
    # Two planner calls happened
    assert len(client.calls) == 2


async def test_graph_handles_unknown_tool(resident):
    """If the model picks a non-existent tool, the observation is an error JSON
    and the next planner turn can pivot. Here we just verify the loop continues."""
    client = ScriptedClient([
        "Thought: oops\nAction: nonexistent_tool\nAction Input: {}",
        "Thought: that didn't work\nFinal Answer: Stopping.",
    ])
    graph = build_agent_graph(client=client, tools=ALL_TOOLS)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("?")]},
        config={"configurable": {"request_id": "graph-test-4", "actor": "tester"}},
    )
    assert state["done"] is True
    # The observation for the bogus tool call should have been appended.
    msgs = state["messages"]
    observations = [m for m in msgs if isinstance(m, HumanMessage) and "Observation" in (m.content or "")]
    assert any("unknown_tool" in (m.content or "") for m in observations)
