"""ReAct output parser."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field


class AgentAction(BaseModel):
    tool: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    thought: str | None = None
    raw: str


class AgentFinish(BaseModel):
    output: str
    thought: str | None = None
    raw: str


class ParseError(Exception):
    """The model's response didn't match the ReAct contract."""


_FINAL_RE = re.compile(r"Final\s*Answer\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
_ACTION_RE = re.compile(r"Action\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_INPUT_RE = re.compile(
    r"Action\s*Input\s*:\s*(.+?)(?=\n\s*(?:Thought|Action|Observation|Final\s*Answer)\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_THOUGHT_RE = re.compile(r"Thought\s*:\s*(.+?)(?=\n\s*(?:Action|Final\s*Answer)\s*:|\Z)",
                         re.IGNORECASE | re.DOTALL)
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


_REACT_KEYWORDS = ("Final Answer", "Action Input", "Action", "Thought", "Observation")


def _strip_markdown(text: str) -> str:
    """Normalize bolded ReAct keywords."""
    for kw in _REACT_KEYWORDS:
        text = re.sub(
            rf"\*+\s*({re.escape(kw)})\s*:\s*\*+",
            r"\1:",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            rf"\*+\s*({re.escape(kw)})\s*\*+\s*:",
            r"\1:",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _extract_json(blob: str) -> dict[str, Any]:
    """Parse the Action Input chunk into a dict."""
    cleaned = _CODE_FENCE_RE.sub("", blob).strip()
    if not cleaned:
        return {}

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}

    candidate = cleaned[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate.replace("'", '"'))
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise ParseError(f"Action Input is not valid JSON: {candidate[:200]}") from exc


_OBSERVATION_RE = re.compile(r"\bObservation\s*:", re.IGNORECASE)


def parse_react_output(text: str) -> AgentAction | AgentFinish:
    """Convert raw LLM text into a typed ReAct decision."""
    if not text or not text.strip():
        raise ParseError("empty model output")

    stripped = _strip_markdown(text)

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
