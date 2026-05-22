"""Tests for the agent layer."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.agent.graph import (
    _consecutive_tool_calls_without_draft,
    _drafted_themes,
    _expected_themes,
    build_agent_graph,
)
from app.agent.llm_tools import (
    ALL_TOOLS,
    draft_sis_entry as draft_sis_entry_tool,
    get_resident as get_resident_tool,
)
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
    """Parses bolded ReAct keywords."""
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
    """Prefers the first ReAct marker."""
    text = """\
Action: get_resident
Action Input: {"name_or_id": "x"}
Final Answer: Already done.
"""
    out = parse_react_output(text)
    assert isinstance(out, AgentAction)
    assert out.tool == "get_resident"


def test_parser_truncates_at_hallucinated_observation():
    """Ignores model-written Observation text."""
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


def _graph_config(thread_id: str, request_id: str = REQUEST_ID, actor: str = "tester") -> dict:
    return {
        "configurable": {
            "request_id": request_id,
            "actor": actor,
            "thread_id": thread_id,
        },
    }


def _fresh_graph(client, tools=None):
    return build_agent_graph(
        client=client,
        tools=tools if tools is not None else ALL_TOOLS,
        checkpointer=MemorySaver(),
    )


async def test_graph_runs_get_resident_then_finishes(resident):
    """Two-step plan: resolve resident → declare done."""
    rid = str(resident.id)
    client = ScriptedClient([
        f"""\
Thought: resolve the resident first.
Action: get_resident
Action Input: {{"name_or_id": "Müller"}}
""",
        """\
Thought: I have the id; nothing else asked.
Final Answer: Resolved Frau Müller (room 12). No documentation requested.
""",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("who is Müller in room 12?")]},
        config=_graph_config("t-1", request_id="graph-test-1"),
    )

    assert state["done"] is True
    assert "Müller" in state["final_answer"]

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
    graph = _fresh_graph(client)
    await graph.ainvoke(
        {"messages": [HumanMessage("BP 130/82, pulse 72 for Frau Müller.")]},
        config=_graph_config("t-2", request_id="graph-test-2"),
    )

    drafts = await CareEvent.filter(request_id="graph-test-2").all()
    assert len(drafts) == 1
    assert drafts[0].theme == Theme.VITALS
    assert drafts[0].status == EventStatus.DRAFT
    assert drafts[0].content["bp_systolic"] == 130


async def test_graph_recovers_from_parse_error(resident):
    client = ScriptedClient([
        "I'll just chat about this for a bit.",
        "Thought: ok.\nFinal Answer: Recovered.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("?")]},
        config=_graph_config("t-3", request_id="graph-test-3"),
    )
    assert state["done"] is True
    assert "Recovered" in state["final_answer"]
    assert len(client.calls) == 2


async def test_graph_handles_unknown_tool(resident):
    client = ScriptedClient([
        "Thought: oops\nAction: nonexistent_tool\nAction Input: {}",
        "Thought: that didn't work\nFinal Answer: Stopping.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("?")]},
        config=_graph_config("t-4", request_id="graph-test-4"),
    )
    assert state["done"] is True
    msgs = state["messages"]
    observations = [m for m in msgs if isinstance(m, HumanMessage) and "Observation" in (m.content or "")]
    assert any("unknown_tool" in (m.content or "") for m in observations)


async def test_ask_caregiver_interrupts_graph(resident, other_resident):
    """Pauses the graph on caregiver questions."""
    client = ScriptedClient([
        """\
Thought: two residents named Müller — disambiguate first.
Action: ask_caregiver
Action Input: {"question": "Which Müller — Margarethe (room 12) or Hans (room 14)?"}
""",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("BP 128/78 for Müller.")]},
        config=_graph_config("t-ask-1", request_id="graph-ask-1"),
    )
    interrupts = state.get("__interrupt__") or []
    assert interrupts, "graph should have paused on ask_caregiver"
    value = interrupts[0].value
    assert "Margarethe" in value["question"] and "Hans" in value["question"]
    assert state.get("done") is not True


async def test_resume_continues_with_reply(resident, other_resident):
    """Continues the graph after a caregiver reply."""
    client = ScriptedClient([
        """\
Thought: ambiguous — ask.
Action: ask_caregiver
Action Input: {"question": "Which Müller?"}
""",
        """\
Thought: caregiver said Margarethe. Done for now.
Final Answer: Got it — Margarethe Müller (room 12).
""",
    ])
    graph = _fresh_graph(client)
    config = _graph_config("t-ask-2", request_id="graph-ask-2")

    first = await graph.ainvoke(
        {"messages": [HumanMessage("Just finished with Müller.")]},
        config=config,
    )
    assert (first.get("__interrupt__") or []), "should pause first"

    final = await graph.ainvoke(Command(resume="Margarethe"), config=config)
    assert final.get("done") is True
    assert "Margarethe" in final["final_answer"]

    obs_msgs = [
        m for m in final["messages"]
        if isinstance(m, HumanMessage) and "Observation" in (m.content or "")
    ]
    assert any("Margarethe" in (m.content or "") for m in obs_msgs)


def test_expected_themes_detects_vitals_and_nutrition():
    msgs = [HumanMessage("BP 130/82 and ate breakfast")]
    assert _expected_themes(msgs) == {"vitals", "nutrition"}


def test_expected_themes_query_only_returns_empty():
    msgs = [HumanMessage("who is in room 12?")]
    assert _expected_themes(msgs) == set()


def test_drafted_themes_walks_observations():
    msgs = [
        HumanMessage("BP 130/82"),
        HumanMessage(content='Observation: {"entry_id": "e1", "theme": "vitals"}'),
        HumanMessage(content='Observation: {"status": "resolved", "resident": {"id": "abc"}}'),
        HumanMessage(content='Observation: {"entry_id": "e2", "theme": "nutrition"}'),
    ]
    assert _drafted_themes(msgs) == {"vitals", "nutrition"}


def test_consecutive_tool_calls_resets_at_draft():
    msgs = [
        HumanMessage(content='Observation: {"status": "resolved"}'),
        HumanMessage(content='Observation: {"entry_id": "e1", "theme": "vitals"}'),
        HumanMessage(content='Observation: {"flags": []}'),
        HumanMessage(content='Observation: {"events": []}'),
    ]
    assert _consecutive_tool_calls_without_draft(msgs) == 2


def test_consecutive_tool_calls_no_draft_seen():
    msgs = [
        HumanMessage(content='Observation: {"status": "resolved"}'),
        HumanMessage(content='Observation: {"flags": []}'),
    ]
    assert _consecutive_tool_calls_without_draft(msgs) == 2


async def test_reflection_rejects_when_themes_missing(resident):
    """Rejects a final answer when transcript themes are missing."""
    rid = str(resident.id)
    client = ScriptedClient([
        f'Thought: resolve.\nAction: get_resident\nAction Input: {{"name_or_id": "Müller"}}',
        (
            f'Thought: draft vitals.\nAction: draft_sis_entry\nAction Input: '
            f'{{"theme": "vitals", "resident_id": "{rid}", '
            f'"content": {{"bp_systolic": 130, "bp_diastolic": 82}}, '
            f'"source_transcript": "BP 130/82"}}'
        ),
        "Thought: done.\nFinal Answer: Documented vitals.",
        (
            f'Thought: forgot nutrition.\nAction: draft_sis_entry\nAction Input: '
            f'{{"theme": "nutrition", "resident_id": "{rid}", '
            f'"content": {{"meals": [{{"meal": "breakfast", "intake_pct": 100, "refused": false}}]}}, '
            f'"source_transcript": "ate breakfast"}}'
        ),
        "Thought: now truly done.\nFinal Answer: Documented vitals and nutrition.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("BP 130/82, ate breakfast for Müller.")]},
        config=_graph_config("t-reflect", request_id="graph-reflect"),
    )

    assert state["done"] is True
    assert "vitals" in state["final_answer"].lower()
    assert "nutrition" in state["final_answer"].lower()

    reflect_msgs = [
        m for m in state["messages"]
        if isinstance(m, HumanMessage) and "REFLECTION CHECK" in (m.content or "")
    ]
    assert reflect_msgs, "expected one reflection rejection"

    drafts = await CareEvent.filter(request_id="graph-reflect").all()
    themes = {d.theme for d in drafts}
    assert Theme.VITALS in themes
    assert Theme.NUTRITION in themes


async def test_reflection_passes_when_all_themes_drafted(resident):
    """Accepts a final answer when all themes are drafted."""
    rid = str(resident.id)
    client = ScriptedClient([
        f'Thought: resolve.\nAction: get_resident\nAction Input: {{"name_or_id": "Müller"}}',
        (
            f'Thought: vitals.\nAction: draft_sis_entry\nAction Input: '
            f'{{"theme": "vitals", "resident_id": "{rid}", '
            f'"content": {{"bp_systolic": 130, "bp_diastolic": 82}}, '
            f'"source_transcript": "BP 130/82"}}'
        ),
        "Thought: done.\nFinal Answer: Documented vitals.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("BP 130/82 for Müller.")]},
        config=_graph_config("t-reflect-ok", request_id="graph-reflect-ok"),
    )
    assert state["done"] is True
    reflect_msgs = [
        m for m in state["messages"]
        if isinstance(m, HumanMessage) and "REFLECTION CHECK" in (m.content or "")
    ]
    assert not reflect_msgs


async def test_validator_retry_hint_added_on_failure(resident):
    """Adds a retry hint after validation failure."""
    rid = str(resident.id)
    seed_config = {"configurable": {"request_id": "preseed", "actor": "agent"}}
    drafted = await draft_sis_entry_tool.ainvoke(
        {
            "theme": "vitals",
            "resident_id": rid,
            "content": {"bp_systolic": 145, "bp_diastolic": 92, "heart_rate": 78},
            "source_transcript": "no numbers here at all",
        },
        config=seed_config,
    )
    entry_id = drafted["entry_id"]

    client = ScriptedClient([
        (
            f'Thought: validate.\nAction: validate_entry\n'
            f'Action Input: {{"entry_id": "{entry_id}", "source_transcript": "no numbers here at all"}}'
        ),
        "Thought: stop here.\nFinal Answer: Stopped.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("please validate that entry")]},
        config=_graph_config("t-retry", request_id="graph-retry"),
    )
    obs = [
        m for m in state["messages"]
        if isinstance(m, HumanMessage) and "Observation" in (m.content or "")
    ]
    hint_msgs = [m for m in obs if "_retry_hint" in (m.content or "")]
    assert hint_msgs, "validate_entry failure should attach _retry_hint to observation"
    assert "1/2" in hint_msgs[0].content


async def test_validator_gives_up_after_two_failures(resident):
    """Adds a give-up hint after repeated validation failures."""
    rid = str(resident.id)
    seed_config = {"configurable": {"request_id": "preseed", "actor": "agent"}}
    drafted = await draft_sis_entry_tool.ainvoke(
        {
            "theme": "vitals",
            "resident_id": rid,
            "content": {"bp_systolic": 145, "bp_diastolic": 92},
            "source_transcript": "no numbers here at all",
        },
        config=seed_config,
    )
    entry_id = drafted["entry_id"]

    validate_step = (
        f'Thought: validate.\nAction: validate_entry\n'
        f'Action Input: {{"entry_id": "{entry_id}", "source_transcript": "no numbers here at all"}}'
    )
    client = ScriptedClient([
        validate_step,
        validate_step,
        validate_step,
        "Thought: ok stop.\nFinal Answer: Stopped.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("please validate")]},
        config=_graph_config("t-giveup", request_id="graph-giveup"),
    )
    obs_texts = [
        m.content for m in state["messages"]
        if isinstance(m, HumanMessage) and "Observation" in (m.content or "")
    ]
    retry_hits = sum("_retry_hint" in t for t in obs_texts)
    giveup_hits = sum("_give_up_hint" in t for t in obs_texts)
    assert retry_hits == 2, f"expected 2 retry hints, got {retry_hits}: {obs_texts}"
    assert giveup_hits == 1, f"expected 1 give-up hint, got {giveup_hits}: {obs_texts}"


async def test_stuck_hint_injected_after_many_non_draft_calls(resident):
    """Adds a stuck hint after repeated non-draft tool calls."""
    rid = str(resident.id)
    lookup = lambda n=rid: f'Action: get_recent_notes\nAction Input: {{"resident_id": "{n}"}}'
    client = ScriptedClient([
        f'Action: get_resident\nAction Input: {{"name_or_id": "Müller"}}',
        lookup(),
        f'Action: search_care_plan\nAction Input: {{"resident_id": "{rid}"}}',
        (
            f'Action: check_vital_ranges\nAction Input: '
            f'{{"resident_id": "{rid}", "vitals": {{"bp_systolic": 130}}}}'
        ),
        lookup(),
        lookup(),
        (
            f'Action: draft_sis_entry\nAction Input: '
            f'{{"theme": "vitals", "resident_id": "{rid}", '
            f'"content": {{"bp_systolic": 130, "bp_diastolic": 82}}, '
            f'"source_transcript": "BP 130/82"}}'
        ),
        "Final Answer: Documented vitals.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("BP 130/82 for Frau Müller.")]},
        config=_graph_config("t-stuck", request_id="graph-stuck"),
    )
    stuck_obs = [
        m for m in state["messages"]
        if isinstance(m, HumanMessage) and "_stuck_hint" in (m.content or "")
    ]
    assert stuck_obs, "expected _stuck_hint after 6 non-draft tool calls"


async def test_stuck_hint_not_injected_on_query_transcript(resident):
    """Skips stuck hints for query-only transcripts."""
    rid = str(resident.id)
    lookup = lambda: f'Action: get_recent_notes\nAction Input: {{"resident_id": "{rid}"}}'
    client = ScriptedClient([
        f'Action: get_resident\nAction Input: {{"name_or_id": "Müller"}}',
        lookup(),
        lookup(),
        lookup(),
        lookup(),
        lookup(),
        lookup(),
        "Final Answer: All quiet on that shift.",
    ])
    graph = _fresh_graph(client)
    state = await graph.ainvoke(
        {"messages": [HumanMessage("any recent notes on Müller?")]},
        config=_graph_config("t-stuck-query", request_id="graph-stuck-q"),
    )
    stuck_obs = [
        m for m in state["messages"]
        if isinstance(m, HumanMessage) and "_stuck_hint" in (m.content or "")
    ]
    assert not stuck_obs


async def test_ask_caregiver_writes_audit_row_before_pausing(resident, other_resident):
    """Writes audit rows before graph suspension."""
    client = ScriptedClient([
        """\
Thought: ambiguous.
Action: ask_caregiver
Action Input: {"question": "Which Müller?"}
""",
    ])
    graph = _fresh_graph(client)
    await graph.ainvoke(
        {"messages": [HumanMessage("BP 128/78 for Müller.")]},
        config=_graph_config("t-ask-3", request_id="graph-ask-3"),
    )
    rows = await AuditLog.filter(request_id="graph-ask-3", action="tool.ask_caregiver").all()
    assert len(rows) == 1
    assert "Which Müller" in rows[0].payload["input"]["question"]
