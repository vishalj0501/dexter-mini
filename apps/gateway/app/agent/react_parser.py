"""ReAct output parser.

Replicate (and most non-OpenAI proxies) don't accept a `tools` parameter, so
we can't ask the model to emit OpenAI-style structured tool_calls. Instead we
prompt the model to produce text in the classic ReAct format:

    Thought: <reasoning>
    Action: <tool_name>
    Action Input: {"k": "v", ...}

…or, when it's done:

    Thought: <reasoning>
    Final Answer: <human-readable wrap-up>

This module turns that text into a typed `AgentAction` or `AgentFinish`, with
enough tolerance to absorb common LLM quirks (markdown bolding, code fences
around the JSON, trailing prose).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field


class AgentAction(BaseModel):
    tool: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    thought: str | None = None
    raw: str  # the original LLM text, for audit/debug


class AgentFinish(BaseModel):
    output: str
    thought: str | None = None
    raw: str


class ParseError(Exception):
    """The model's response didn't match the ReAct contract."""


# Match "Final Answer:" through end-of-string (or until another marker, but
# Final Answer is terminal so we take the rest).
_FINAL_RE = re.compile(r"Final\s*Answer\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)

# Action line — the tool name on the same line as "Action:".
_ACTION_RE = re.compile(r"Action\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)

# Action Input — everything from "Action Input:" up to the next ReAct keyword
# (or end of text). DOTALL so multi-line JSON works.
_INPUT_RE = re.compile(
    r"Action\s*Input\s*:\s*(.+?)(?=\n\s*(?:Thought|Action|Observation|Final\s*Answer)\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# Optional thought line — kept for audit / UI display.
_THOUGHT_RE = re.compile(r"Thought\s*:\s*(.+?)(?=\n\s*(?:Action|Final\s*Answer)\s*:|\Z)",
                         re.IGNORECASE | re.DOTALL)

# Strip these — they show up around the JSON often.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


_REACT_KEYWORDS = ("Final Answer", "Action Input", "Action", "Thought", "Observation")


def _strip_markdown(text: str) -> str:
    """Normalise ReAct keywords that come back wrapped in markdown bolding.

    Handles `**Action:**`, `**Action**:`, and the plain `Action:` form.
    Longest keywords first so `Action Input` wins over `Action`.
    """
    for kw in _REACT_KEYWORDS:
        # `**Keyword:**` → `Keyword:`
        text = re.sub(
            rf"\*+\s*({re.escape(kw)})\s*:\s*\*+",
            r"\1:",
            text,
            flags=re.IGNORECASE,
        )
        # `**Keyword**:` → `Keyword:`
        text = re.sub(
            rf"\*+\s*({re.escape(kw)})\s*\*+\s*:",
            r"\1:",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _extract_json(blob: str) -> dict[str, Any]:
    """Parse the Action Input chunk into a dict.

    Tolerates: code fences, surrounding whitespace, single quotes, trailing
    prose after the JSON object. Returns empty dict if no JSON object is
    present (the tool may genuinely take zero args).
    """
    cleaned = _CODE_FENCE_RE.sub("", blob).strip()
    if not cleaned:
        return {}

    # Greedy match: from the first '{' to the LAST matching '}'. JSON objects
    # in tool inputs are flat for our 13 tools, so this is safe.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        # Maybe it's already a bare key/value pair like name=value? Not a
        # format we support — surface as empty so the planner can be re-prompted.
        return {}

    candidate = cleaned[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Last-ditch repair: replace single quotes with double, strip trailing commas.
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate.replace("'", '"'))
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ParseError(f"Action Input is not valid JSON: {candidate[:200]}") from exc


_OBSERVATION_RE = re.compile(r"\bObservation\s*:", re.IGNORECASE)


def parse_react_output(text: str) -> AgentAction | AgentFinish:
    """Convert raw LLM text into a typed decision.

    Key defence against a common prompted-ReAct failure mode: the model
    role-plays the *whole* trajectory in one response — writing fake
    "Observation:" lines for itself and then a Final Answer. We refuse to
    believe anything after the first "Observation:" marker (that's a
    hallucination — the real observations come from us), and we resolve
    Action-vs-Final-Answer by **whichever marker appears first** in the
    truncated text. Generation order is the source of truth.

    Raises `ParseError` if neither marker is present.
    """
    if not text or not text.strip():
        raise ParseError("empty model output")

    stripped = _strip_markdown(text)

    # Discard everything after the first Observation: marker — the real
    # observations are appended by the tools node, not by the model.
    obs = _OBSERVATION_RE.search(stripped)
    if obs:
        stripped = stripped[: obs.start()]

    action_match = _ACTION_RE.search(stripped)
    final_match = _FINAL_RE.search(stripped)

    if not action_match and not final_match:
        raise ParseError(
            "no `Action:` or `Final Answer:` found in model output. "
            "Got: " + (stripped[:200].replace("\n", " ") + "…")
        )

    # First marker wins (matches generation order).
    final_first = final_match is not None and (
        action_match is None or final_match.start() < action_match.start()
    )

    thought_match = _THOUGHT_RE.search(stripped)
    thought = thought_match.group(1).strip() if thought_match else None

    if final_first:
        return AgentFinish(output=final_match.group(1).strip(), thought=thought, raw=text)

    input_match = _INPUT_RE.search(stripped)
    tool_input = _extract_json(input_match.group(1)) if input_match else {}
    return AgentAction(
        tool=action_match.group(1).strip(),
        tool_input=tool_input,
        thought=thought,
        raw=text,
    )


__all__ = ["AgentAction", "AgentFinish", "ParseError", "parse_react_output"]
