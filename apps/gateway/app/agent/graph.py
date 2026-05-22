"""Hand-wired ReAct graph for planner and tool execution."""

from __future__ import annotations

import json
import logging
import operator
import re
from functools import lru_cache
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from app.agent.llm_tools import ALL_TOOLS
from app.agent.prompts import build_system_prompt
from app.agent.react_parser import AgentAction, AgentFinish, ParseError, parse_react_output
from app.llm import Completion, LLMClient, get_client

log = logging.getLogger(__name__)


def _merge_validation_failures(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    """Reducer that sums per-entry validation failure counts."""
    out = dict(left or {})
    for k, v in (right or {}).items():
        out[k] = out.get(k, 0) + (v if isinstance(v, int) else 1)
    return out


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    pending_action: dict[str, Any] | None
    iteration: int
    done: bool
    final_answer: str | None
    drafts_created: Annotated[list[str], operator.add]
    finish_attempts: Annotated[int, operator.add]
    validation_failures: Annotated[dict[str, int], _merge_validation_failures]


def _ctx(config: RunnableConfig | None) -> tuple[str, str]:
    cfg = (config or {}).get("configurable") or {}
    request_id = cfg.get("request_id")
    if not request_id:
        raise ValueError(
            "agent graph called without request_id; pass it via "
            "config={'configurable': {'request_id': ...}}"
        )
    return request_id, cfg.get("actor", "agent")


def _to_litellm_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """Convert LangChain messages to LiteLLM's OpenAI-shaped dicts."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({"role": "system", "content": m.content})
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": m.content})
        else:
            out.append({"role": "user", "content": str(m.content)})
    return out


def _tools_by_name(tools: list[Any]) -> dict[str, Any]:
    return {t.name: t for t in tools}


_DOC_KEYWORDS = re.compile(
    r"\b(bp|pulse|heart\s*rate|temp|temperature|o2|sat|ate|eat|refused|"
    r"breakfast|lunch|dinner|meal|hydration|walk|walked|walking|fell|fall|"
    r"mobility|mood|cognition|incident|nausea|pain|sleep|slept)\b",
    re.IGNORECASE,
)


def _looks_like_documentation(messages: list[BaseMessage]) -> bool:
    """Return whether the first human input looks like documentation."""
    if not messages:
        return False
    first = next((m for m in messages if isinstance(m, HumanMessage)), None)
    if not first:
        return False
    text = first.content or ""
    return bool(_DOC_KEYWORDS.search(text))


_THEME_KEYWORDS: dict[str, re.Pattern[str]] = {
    "vitals": re.compile(
        r"\b(bp|pulse|heart\s*rate|hr|temp(?:erature)?|o2|sat(?:uration)?|"
        r"blood\s*pressure|weight)\b",
        re.IGNORECASE,
    ),
    "nutrition": re.compile(
        r"\b(ate|eat|eating|refused|breakfast|lunch|dinner|meal|appetite|"
        r"hydration|fluid|drink|drank)\b",
        re.IGNORECASE,
    ),
    "mobility": re.compile(
        r"\b(walk|walked|walking|fell|fall|falls|mobility|aid|walker|"
        r"wheelchair|gait|stand|standing|transfer)\b",
        re.IGNORECASE,
    ),
    "cognition": re.compile(
        r"\b(orient(?:ed|ation)?|confus(?:ed|ion)|memory|cognition|mood|"
        r"agitat|alert)\b",
        re.IGNORECASE,
    ),
    "incident": re.compile(
        r"\b(incident|injur(?:y|ed)|emergency|bleeding|seizure)\b",
        re.IGNORECASE,
    ),
}


def _expected_themes(messages: list[BaseMessage]) -> set[str]:
    """Themes the original transcript mentions."""
    first = next((m for m in messages if isinstance(m, HumanMessage)), None)
    if not first:
        return set()
    text = first.content or ""
    return {t for t, pat in _THEME_KEYWORDS.items() if pat.search(text)}


def _drafted_themes(messages: list[BaseMessage]) -> set[str]:
    """Themes for which we saw a real draft_sis_entry Observation (entry_id + theme)."""
    themes: set[str] = set()
    for m in messages:
        if not isinstance(m, HumanMessage):
            continue
        text = m.content or ""
        if "Observation:" not in text:
            continue
        try:
            obs_text = text.split("Observation:", 1)[1].strip()
            obs = json.loads(obs_text)
        except (ValueError, IndexError):
            continue
        if isinstance(obs, dict) and obs.get("entry_id") and obs.get("theme"):
            themes.add(str(obs["theme"]))
    return themes


_FLAG_CLAIM_LANGUAGE = re.compile(
    r"\b(?:flagged?|raise[d]?\s+(?:a\s+)?flag|escalat(?:e[ds]?|ing)|flag_for_review)\b"
    r"|\bfor\s+(?:high[- ]severity\s+)?(?:clinical\s+)?review\b",
    re.IGNORECASE,
)


def _iter_observations(messages: list[BaseMessage]):
    """Yield parsed Observation dicts in order (real graph-emitted observations)."""
    for m in messages:
        if not isinstance(m, HumanMessage):
            continue
        text = m.content or ""
        if "Observation:" not in text:
            continue
        try:
            obs_text = text.split("Observation:", 1)[1].strip()
            obs = json.loads(obs_text)
        except (ValueError, IndexError):
            continue
        if isinstance(obs, dict):
            yield obs


def _drafted_entry_ids(messages: list[BaseMessage]) -> set[str]:
    """Entry ids that appear in real draft_sis_entry observations."""
    ids: set[str] = set()
    for obs in _iter_observations(messages):
        if obs.get("entry_id") and obs.get("theme") and "passed" not in obs:
            ids.add(str(obs["entry_id"]))
    return ids


def _validated_entry_ids(messages: list[BaseMessage]) -> set[str]:
    """Entry ids that have a validate_entry observation (regardless of passed)."""
    ids: set[str] = set()
    for obs in _iter_observations(messages):
        if obs.get("entry_id") and "passed" in obs:
            ids.add(str(obs["entry_id"]))
    return ids


def _flag_for_review_called(messages: list[BaseMessage]) -> bool:
    """Did any observation carry a real flag_id?"""
    for obs in _iter_observations(messages):
        if obs.get("flag_id"):
            return True
    return False


def _should_have_flagged(messages: list[BaseMessage]) -> bool:
    """check_vital_ranges returned abnormal, OR watch with a matching plan risk."""
    plan_risks: list[str] = []
    for obs in _iter_observations(messages):
        if "risk_flags" in obs and isinstance(obs.get("risk_flags"), list):
            plan_risks = [str(r).lower() for r in obs["risk_flags"]]
    for obs in _iter_observations(messages):
        overall = obs.get("overall")
        if overall == "abnormal":
            return True
        if overall == "watch" and plan_risks:
            return True
    return False


def _final_answer_claims_flag(text: str) -> bool:
    return bool(_FLAG_CLAIM_LANGUAGE.search(text or ""))


def _implausible_vitals_drafted(messages: list[BaseMessage]) -> bool:
    """Return whether implausible vitals were drafted anyway."""
    seen_implausible = False
    for obs in _iter_observations(messages):
        if obs.get("overall") == "implausible":
            seen_implausible = True
        if (
            seen_implausible
            and obs.get("entry_id")
            and obs.get("theme") == "vitals"
            and "passed" not in obs
        ):
            return True
    return False


def _consecutive_tool_calls_without_draft(messages: list[BaseMessage]) -> int:
    """Count trailing Observation messages until the latest draft."""
    count = 0
    for m in reversed(messages):
        if not isinstance(m, HumanMessage):
            continue
        text = m.content or ""
        if "Observation:" not in text:
            continue
        try:
            obs_text = text.split("Observation:", 1)[1].strip()
            obs = json.loads(obs_text)
        except (ValueError, IndexError):
            count += 1
            continue
        if isinstance(obs, dict) and obs.get("entry_id") and obs.get("theme"):
            return count
        count += 1
    return count


async def _call_planner(
    messages: list[BaseMessage],
    *,
    request_id: str,
    actor: str,
    client: LLMClient,
) -> Completion:
    """One LLM call for the planner role."""
    return await client.complete(
        _to_litellm_messages(messages),
        request_id=request_id,
        actor=actor,
    )


def _make_planner_node(client: LLMClient, system_prompt: str):
    async def planner_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        request_id, actor = _ctx(config)
        msgs: list[BaseMessage] = [SystemMessage(content=system_prompt), *state["messages"]]

        completion = await _call_planner(msgs, request_id=request_id, actor=actor, client=client)
        text = completion.content or ""
        ai = AIMessage(content=text)

        try:
            decision = parse_react_output(text)
        except ParseError as exc:
            log.warning("planner: parse error rid=%s: %s", request_id, exc)
            repair = HumanMessage(
                content=(
                    f"FORMAT ERROR: {exc}\n"
                    f"Please respond using exactly:\n"
                    f"  Thought: ...\n  Action: <tool>\n  Action Input: <json>\n"
                    f"OR\n  Thought: ...\n  Final Answer: ..."
                )
            )
            return {
                "messages": [ai, repair],
                "iteration": state.get("iteration", 0) + 1,
            }

        if isinstance(decision, AgentFinish):
            drafts = state.get("drafts_created", []) or []
            attempts = state.get("finish_attempts", 0)
            needs_drafts = _looks_like_documentation(state["messages"])
            if needs_drafts and not drafts and attempts == 0:
                log.warning(
                    "planner: rejecting premature Final Answer rid=%s — "
                    "no draft_sis_entry calls in this trajectory", request_id,
                )
                reject = HumanMessage(
                    content=(
                        "REJECTED: You wrote Final Answer but you have NOT called "
                        "draft_sis_entry in this conversation. Look at the real "
                        "observations: none of them contain an `entry_id`. The "
                        "drafts you described don't exist in the system.\n\n"
                        "Continue documentation now. Your next message must be:\n"
                        "  Thought: ...\n  Action: draft_sis_entry\n  Action Input: <json>\n"
                        "Do NOT write Final Answer again until you have seen real "
                        "Observations with `entry_id` for each theme."
                    )
                )
                return {
                    "messages": [ai, reject],
                    "iteration": state.get("iteration", 0) + 1,
                    "finish_attempts": 1,
                }
            if drafts and attempts < 2:
                msgs = state["messages"]
                expected = _expected_themes(msgs)
                drafted_th = _drafted_themes(msgs)
                missing_themes = expected - drafted_th

                drafted_ids = _drafted_entry_ids(msgs)
                validated_ids = _validated_entry_ids(msgs)
                unvalidated = drafted_ids - validated_ids

                must_flag = _should_have_flagged(msgs)
                flagged = _flag_for_review_called(msgs)
                claims_flag = _final_answer_claims_flag(decision.output)

                issues: list[str] = []
                if missing_themes:
                    issues.append(
                        f"Missing themes: transcript mentions {sorted(expected)}, "
                        f"you drafted {sorted(drafted_th)}, missing {sorted(missing_themes)}."
                    )
                if unvalidated:
                    issues.append(
                        f"Drafts without validate_entry: {sorted(unvalidated)}. "
                        f"Call validate_entry on each before finishing."
                    )
                if must_flag and not flagged:
                    issues.append(
                        "check_vital_ranges flagged abnormal or watch+plan-risk, "
                        "but flag_for_review was never called. Call it now."
                    )
                if claims_flag and not flagged:
                    issues.append(
                        "Your Final Answer claims you flagged this for review, but no "
                        "flag_for_review tool call appears in the observations. "
                        "Either call flag_for_review (and get a real flag_id) or "
                        "rewrite the Final Answer without claiming a flag."
                    )
                if _implausible_vitals_drafted(msgs):
                    issues.append(
                        "check_vital_ranges returned overall='implausible' but you "
                        "drafted vitals anyway. Silent value-substitution is "
                        "documentation fraud. Skip the vitals draft and call "
                        "ask_caregiver to confirm the real numbers."
                    )

                if issues:
                    log.info(
                        "planner: completion check rejected rid=%s attempt=%d issues=%d",
                        request_id, attempts, len(issues),
                    )
                    bullets = "\n".join(f"  - {i}" for i in issues)
                    reject = HumanMessage(
                        content=(
                            "COMPLETION CHECK before Final Answer — issues found:\n"
                            f"{bullets}\n\n"
                            "Fix each above with the appropriate tool call. Do NOT "
                            "emit Final Answer until each is resolved. Do NOT fabricate "
                            "ids or claim work you have not done."
                        )
                    )
                    return {
                        "messages": [ai, reject],
                        "iteration": state.get("iteration", 0) + 1,
                        "finish_attempts": 1,
                    }
            return {
                "messages": [ai],
                "done": True,
                "final_answer": decision.output,
                "iteration": state.get("iteration", 0) + 1,
                "finish_attempts": 1,
            }

        return {
            "messages": [ai],
            "pending_action": decision.model_dump(),
            "iteration": state.get("iteration", 0) + 1,
        }

    return planner_node


def _make_tools_node(tools: list[Any]):
    by_name = _tools_by_name(tools)

    async def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        action = state.get("pending_action")
        if not action:
            return {"pending_action": None}

        name = action["tool"]
        args = action.get("tool_input") or {}
        tool = by_name.get(name)

        observation: Any
        if tool is None:
            observation = {
                "error": "unknown_tool",
                "message": f"No tool named {name!r}. Available: {sorted(by_name)}",
            }
        else:
            try:
                result = await tool.ainvoke(args, config=config)
                observation = result if isinstance(result, (dict, list)) else {"result": result}
            except GraphBubbleUp:
                raise
            except Exception as exc:
                log.warning("tool %s raised: %s", name, exc)
                observation = {"error": type(exc).__name__, "message": str(exc)}

        updates: dict[str, Any] = {"pending_action": None}
        if name == "draft_sis_entry" and isinstance(observation, dict) and observation.get("entry_id"):
            updates["drafts_created"] = [str(observation["entry_id"])]

        if (
            name == "check_vital_ranges"
            and isinstance(observation, dict)
            and observation.get("overall") == "implausible"
        ):
            observation = {
                **observation,
                "_implausible_hint": (
                    "The values you tried to verify are outside human physiology. "
                    "DO NOT draft a vitals entry. DO NOT substitute your own "
                    "plausible numbers — that is documentation fraud. Call "
                    "ask_caregiver to confirm what the caregiver actually measured."
                ),
            }

        if (
            name == "validate_entry"
            and isinstance(observation, dict)
            and observation.get("passed") is False
        ):
            entry_id = str(observation.get("entry_id") or "")
            prior = (state.get("validation_failures") or {}).get(entry_id, 0)
            new_count = prior + 1
            if entry_id:
                updates["validation_failures"] = {entry_id: 1}
            if new_count <= 2:
                observation = {
                    **observation,
                    "_retry_hint": (
                        f"Validation failed (attempt {new_count}/2). Re-extract this "
                        "theme using ONLY values that appear verbatim in the original "
                        "transcript. Leave any ungrounded field as null. Call "
                        "draft_sis_entry again to create a fresh draft — do NOT call "
                        "validate_entry on the existing failed entry_id again."
                    ),
                }
            else:
                observation = {
                    **observation,
                    "_give_up_hint": (
                        f"This entry has failed validation {new_count} times. Stop "
                        "retrying it. The entry is already flipped to needs_review "
                        "in the database. Call flag_for_review with "
                        "reason='draft repeatedly failed grounding' and "
                        "severity='medium', then move on to other themes or finish."
                    ),
                }

        produced_draft = (
            name == "draft_sis_entry"
            and isinstance(observation, dict)
            and observation.get("entry_id")
        )
        if (
            not produced_draft
            and name != "ask_caregiver"
            and _looks_like_documentation(state["messages"])
        ):
            non_draft_total = _consecutive_tool_calls_without_draft(state["messages"]) + 1
            if non_draft_total > 5 and isinstance(observation, dict):
                observation = {
                    **observation,
                    "_stuck_hint": (
                        f"You've made {non_draft_total} tool calls without producing "
                        "a single draft. The transcript contains documentation. Either "
                        "call draft_sis_entry NOW with the values you already have, or "
                        "call ask_caregiver to obtain a specific missing value. Do not "
                        "keep querying."
                    ),
                }

        obs_text = f"Observation: {json.dumps(observation, default=str)}"
        updates["messages"] = [HumanMessage(content=obs_text)]
        return updates

    return tools_node


def _route_after_planner(state: AgentState) -> str:
    if state.get("done"):
        return END
    if state.get("pending_action"):
        return "tools"
    return "planner"


_default_checkpointer: BaseCheckpointSaver = MemorySaver()


def get_default_checkpointer() -> BaseCheckpointSaver:
    return _default_checkpointer


def build_agent_graph(
    *,
    client: LLMClient | None = None,
    tools: list[Any] | None = None,
    system_prompt: str | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Compile the ReAct graph. Overrides are for tests."""
    tool_list = tools if tools is not None else ALL_TOOLS
    planner_client = client or get_client("planner")
    prompt = system_prompt if system_prompt is not None else build_system_prompt(tool_list)

    g = StateGraph(AgentState)
    g.add_node("planner", _make_planner_node(planner_client, prompt))
    g.add_node("tools", _make_tools_node(tool_list))

    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", _route_after_planner, ["tools", "planner", END])
    g.add_edge("tools", "planner")

    return g.compile(checkpointer=checkpointer or _default_checkpointer)


@lru_cache(maxsize=1)
def get_default_graph() -> CompiledStateGraph:
    return build_agent_graph()


__all__ = ["AgentState", "build_agent_graph", "get_default_graph"]
