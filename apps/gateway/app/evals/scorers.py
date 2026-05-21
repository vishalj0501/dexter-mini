"""Score a CaseResult against its EvalCase expectations.

Each scorer returns a 0.0–1.0 float. The metric names mirror the columns on
the EvalRun table so a run can be persisted directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.evals.cases import EvalCase
from app.evals.runner import CaseResult


# UUID-shaped strings in the Final Answer text.
_UUID_RX = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


@dataclass
class ScoreBreakdown:
    case_id: str
    passed: bool
    tool_selection_accuracy: float
    flagged_when_should_have: float
    hallucination_rate: float
    schema_validity_rate: float
    reliability_rate: float
    notes: list[str]


def score(case: EvalCase, result: CaseResult) -> ScoreBreakdown:
    notes: list[str] = []
    called = set(result.tool_sequence)

    # Tool selection: required tools intersected with actually-called.
    if case.must_call_tools:
        hit = case.must_call_tools & called
        miss = case.must_call_tools - called
        tool_acc = len(hit) / len(case.must_call_tools)
        if miss:
            notes.append(f"missing tools: {sorted(miss)}")
    else:
        tool_acc = 1.0
    if forbidden := (case.must_not_call_tools & called):
        notes.append(f"called forbidden tools: {sorted(forbidden)}")
        tool_acc *= max(0.0, 1.0 - 0.5 * len(forbidden))

    # Flag-when-should: 1 iff expectation matches reality.
    actually_flagged = bool(result.flag_ids)
    flag_score = 1.0 if (case.should_flag == actually_flagged) else 0.0
    if case.should_flag and not actually_flagged:
        notes.append("expected flag_for_review, none raised")
    if not case.should_flag and actually_flagged:
        notes.append(f"unexpected flag(s) raised: {result.flag_ids}")

    # Hallucination: every UUID in the Final Answer must be a real id we
    # produced. Returns "rate" = fraction hallucinated (lower is better),
    # so we report 1 - rate so all scores point the same way.
    real_ids = {str(i) for i in result.drafted_entry_ids} | {str(i) for i in result.flag_ids}
    text = result.final_message or ""
    cited = set(_UUID_RX.findall(text))
    if cited:
        fake = cited - real_ids
        halluc_rate = len(fake) / len(cited)
        if fake:
            notes.append(f"hallucinated ids in Final Answer: {sorted(fake)}")
    else:
        halluc_rate = 0.0
    halluc_score = 1.0 - halluc_rate

    # Schema validity: fraction of validate_entry calls that passed. If the
    # planner never validated, that's a 0 (and tool_selection will already
    # show the missing tool).
    if result.validation_passes:
        schema_score = sum(result.validation_passes) / len(result.validation_passes)
    else:
        schema_score = 0.0

    # Reliability: completed without exception.
    reliability = 1.0 if result.completed else 0.0
    if not result.completed:
        notes.append(f"run errored: {result.error}")

    # "Passed" if every dimension is full marks.
    passed = (
        tool_acc == 1.0
        and flag_score == 1.0
        and halluc_score == 1.0
        and schema_score >= 0.8  # validator can be picky on grounding
        and reliability == 1.0
    )

    return ScoreBreakdown(
        case_id=case.id,
        passed=passed,
        tool_selection_accuracy=tool_acc,
        flagged_when_should_have=flag_score,
        hallucination_rate=halluc_score,
        schema_validity_rate=schema_score,
        reliability_rate=reliability,
        notes=notes,
    )


def aggregate(scores: list[ScoreBreakdown]) -> dict[str, float]:
    """Mean of each metric across the set."""
    if not scores:
        return {}
    n = len(scores)
    return {
        "pass_rate": sum(s.passed for s in scores) / n,
        "tool_selection_accuracy": sum(s.tool_selection_accuracy for s in scores) / n,
        "flagged_when_should_have": sum(s.flagged_when_should_have for s in scores) / n,
        "hallucination_rate": sum(s.hallucination_rate for s in scores) / n,
        "schema_validity_rate": sum(s.schema_validity_rate for s in scores) / n,
        "reliability_rate": sum(s.reliability_rate for s in scores) / n,
    }
