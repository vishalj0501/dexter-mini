"""Golden set of caregiver scenarios.

Each case names a seeded resident (so the agent must resolve them via
get_resident) and the trajectory shape we expect. Keep transcripts terse and
in caregiver English — the agent's first guard already handles language
detection separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalCase:
    id: str
    transcript: str

    # Trajectory expectations
    expected_resident_surname: str
    expected_themes: set[str] = field(default_factory=set)
    must_call_tools: set[str] = field(default_factory=set)  # subset that MUST appear
    must_not_call_tools: set[str] = field(default_factory=set)

    # Outcome expectations
    should_flag: bool = False
    should_ask_caregiver: bool = False
    # The Final Answer must NOT reference any flag/entry id not present in
    # observations. Always checked; this is just an explicit marker.
    no_hallucinated_ids: bool = True


GOLDEN_SET: list[EvalCase] = [
    EvalCase(
        id="muller-normal-vitals",
        transcript=(
            "Frau Müller, room 12. BP 128 over 78, pulse 70. "
            "Ate her full breakfast. Walked the hallway with her walker."
        ),
        expected_resident_surname="Müller",
        expected_themes={"vitals", "nutrition", "mobility"},
        must_call_tools={
            "get_resident", "search_care_plan", "check_vital_ranges",
            "draft_sis_entry", "validate_entry",
        },
        must_not_call_tools={"flag_for_review"},
        should_flag=False,
    ),
    EvalCase(
        id="muller-elevated-bp-and-refusal",
        transcript=(
            "Frau Müller, room 12. BP 165 over 102, pulse 88. "
            "Looks tired, refused her breakfast."
        ),
        expected_resident_surname="Müller",
        expected_themes={"vitals", "nutrition"},
        must_call_tools={
            "get_resident", "search_care_plan", "check_vital_ranges",
            "draft_sis_entry", "validate_entry", "flag_for_review",
        },
        should_flag=True,
    ),
    EvalCase(
        id="weber-fall-risk-unaccompanied",
        transcript=(
            "Ingrid Weber, room 18. Found her walking the corridor by herself, "
            "she seemed confused about where her room was."
        ),
        expected_resident_surname="Weber",
        expected_themes={"mobility", "cognition"},
        must_call_tools={
            "get_resident", "search_care_plan", "draft_sis_entry",
            "validate_entry", "flag_for_review",
        },
        should_flag=True,  # wandering_risk + fall_risk on plan
    ),
    EvalCase(
        id="schmidt-normal-routine",
        transcript=(
            "Herr Schmidt, room 14. Walked to the dining room on his own. "
            "Ate his lunch fully."
        ),
        expected_resident_surname="Schmidt",
        expected_themes={"mobility", "nutrition"},
        must_call_tools={
            "get_resident", "draft_sis_entry", "validate_entry",
        },
        must_not_call_tools={"flag_for_review"},
        should_flag=False,
    ),
    EvalCase(
        id="fischer-critical-bp",
        transcript=(
            "Frau Fischer, room 27. BP 188 over 115, pulse 102. "
            "Complained of pain in her chest."
        ),
        expected_resident_surname="Fischer",
        expected_themes={"vitals"},
        must_call_tools={
            "get_resident", "search_care_plan", "check_vital_ranges",
            "draft_sis_entry", "validate_entry", "flag_for_review",
        },
        should_flag=True,  # critical BP + CHF/hypertension on plan
    ),
]
