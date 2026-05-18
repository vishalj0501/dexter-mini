"""LangChain @tool wrappers over the audited tool catalog.

Each wrapper is a thin shell around its sibling in `app/tools/`:
  - Takes only the domain args the LLM should pick.
  - Hides `request_id` and `actor` behind `config: RunnableConfig`, which
    LangChain auto-injects from the graph runtime — the LLM cannot see or
    forge them.
  - Returns the underlying Pydantic model dumped to a dict so the
    `ToolMessage` content serialises cleanly back into the LLM context.

Docstrings here are part of the LLM's prompt (LangChain serialises them into
the tool-spec `description`). Keep them sharp; include *when* to use each
tool, not just *what* it does — that's what shapes tool selection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.schemas.enums import FlagSeverity, Theme
from app.tools import drafting as _drafting
from app.tools import resident as _resident
from app.tools import workflow as _workflow



def _ctx(config: RunnableConfig | None) -> tuple[str, str]:
    """Pull request_id + actor from the runtime config.

    Raises if request_id is missing — that's a programmer bug (every agent
    invocation must pass it via `config={"configurable": {"request_id": ...}}`).
    """
    cfg = (config or {}).get("configurable") or {}
    request_id = cfg.get("request_id")
    if not request_id:
        raise ValueError(
            "agent tool called without request_id; pass it via "
            "config={'configurable': {'request_id': ...}}"
        )
    return request_id, cfg.get("actor", "agent")


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"expected a UUID string, got {value!r}") from exc


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"expected ISO 8601 datetime, got {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt



@tool
async def get_resident(name_or_id: str, config: RunnableConfig = None) -> dict:
    """Resolve a free-form reference (first name, surname, room number, or UUID)
    to a specific resident in the facility.

    ALWAYS call this first when the caregiver mentions someone by name.

    Returns one of three shapes:
      - status="resolved" with a `resident` object → use its `id` for further calls.
      - status="ambiguous" with `candidates` (multiple matches) → call `ask_caregiver`
        to disambiguate.
      - status="not_found" → call `ask_caregiver` to clarify.

    Args:
        name_or_id: A name, surname, room number, or resident UUID. Honorifics
            like "Frau" or "Herr" are stripped automatically.
    """
    request_id, actor = _ctx(config)
    result = await _resident.get_resident(name_or_id, request_id=request_id, actor=actor)
    return result.model_dump(mode="json")


@tool
async def get_recent_notes(
    resident_id: str,
    days: int = 7,
    config: RunnableConfig = None,
) -> dict:
    """Fetch a resident's care events from the last N days, newest first.

    Use this after `get_resident` has resolved an id, to ground decisions in
    history (e.g. "she refused breakfast again" → check what was logged
    yesterday). Includes drafts as well as finalised entries.

    Args:
        resident_id: The resident's UUID, as a string.
        days: How many days back to fetch (default 7, min 1).
    """
    request_id, actor = _ctx(config)
    result = await _resident.get_recent_notes(
        _parse_uuid(resident_id), days, request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def search_care_plan(resident_id: str, config: RunnableConfig = None) -> dict:
    """Fetch a resident's active care plan: goals, risk flags, dietary
    restrictions, mobility status.

    Use this to inform decisions about flagging risks (e.g. fall_risk on the
    plan + observed unsupervised walking → call `flag_for_review`).

    Args:
        resident_id: The resident's UUID, as a string.
    """
    request_id, actor = _ctx(config)
    result = await _resident.search_care_plan(
        _parse_uuid(resident_id), request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def check_vital_ranges(
    resident_id: str,
    vitals: dict[str, Any],
    config: RunnableConfig = None,
) -> dict:
    """Sanity-check a set of vital signs against clinical bands AND the
    resident's baseline. Returns per-field flags and an overall verdict
    ("normal" | "watch" | "abnormal").

    Call this BEFORE drafting a Vitals entry whenever the caregiver reports
    numeric measurements — the result tells you whether to also call
    `flag_for_review`.

    Args:
        resident_id: The resident's UUID, as a string.
        vitals: A dict of measured vitals — any of: bp_systolic, bp_diastolic,
            heart_rate, temperature_c, o2_sat.
    """
    request_id, actor = _ctx(config)
    result = await _resident.check_vital_ranges(
        _parse_uuid(resident_id), vitals, request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")



@tool
async def draft_sis_entry(
    theme: str,
    resident_id: str,
    content: dict[str, Any],
    source_transcript: str,
    config: RunnableConfig = None,
) -> dict:
    """Create a structured SIS draft entry for ONE theme.

    Persists the draft (status=draft) and returns the entry id so you can
    validate it next. Only fill fields explicitly mentioned in the transcript —
    leave anything not stated as null.

    Args:
        theme: One of: "vitals", "nutrition", "mobility", "cognition",
            "social", "incident".
        resident_id: The resident's UUID, as a string.
        content: Fields matching the theme's schema. For "vitals" → {bp_systolic,
            bp_diastolic, heart_rate, temperature_c, o2_sat, ...}. For
            "nutrition" → {meals: [{meal, intake_pct, refused, reason}], ...}.
            See the SIS schemas for full shape.
        source_transcript: The verbatim transcript span this draft is derived
            from. Used by `validate_entry` to check grounding.
    """
    request_id, actor = _ctx(config)
    result = await _drafting.draft_sis_entry(
        Theme(theme), _parse_uuid(resident_id), content, source_transcript,
        request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def validate_entry(
    entry_id: str,
    source_transcript: str,
    config: RunnableConfig = None,
) -> dict:
    """Check whether the entry's filled fields are grounded in the transcript.

    Returns per-field confidence and an overall `passed` boolean. On failure,
    the entry's status is automatically flipped to `needs_review`.

    ALWAYS validate after drafting. If `passed` is false, re-extract with a
    tighter prompt or call `ask_caregiver`.

    Args:
        entry_id: The draft entry's UUID, as a string.
        source_transcript: The transcript span this entry was derived from.
    """
    request_id, actor = _ctx(config)
    result = await _drafting.validate_entry(
        _parse_uuid(entry_id), source_transcript, request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def synthesize_summary(entry_ids: list[str], config: RunnableConfig = None) -> dict:
    """Render confirmed drafts as a multi-paragraph SIS narrative, organised by
    theme and led by a headline.

    Call this at the end of a documentation flow once all drafts have been
    validated and (ideally) confirmed.

    Args:
        entry_ids: List of draft entry UUIDs to include, as strings.
    """
    request_id, actor = _ctx(config)
    uuids = [_parse_uuid(e) for e in entry_ids]
    result = await _drafting.synthesize_summary(uuids, request_id=request_id, actor=actor)
    return result.model_dump(mode="json")


@tool
async def redact_pii(
    text: str,
    extra_names: list[str] | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Replace known resident names in `text` with opaque tokens
    (`[RESIDENT_1]`, etc.) and return the redacted text plus a token→name
    mapping. Use this before sending free text to an external service.

    Args:
        text: The text to redact.
        extra_names: Additional names to redact beyond the resident table.
    """
    request_id, actor = _ctx(config)
    result = await _drafting.redact_pii(
        text, extra_names=extra_names or None, request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")




@tool
async def ask_caregiver(
    question: str,
    context: dict[str, Any] | None = None,
    config: RunnableConfig = None,
) -> dict:
    """Surface a clarifying question to the caregiver. Use this WHENEVER the
    ambiguity is clinically meaningful:
      - `get_resident` returned multiple candidates.
      - A required vital is missing AND yesterday's reading was elevated.
      - A field's value contradicts care-plan history.

    DO NOT use this for purely cosmetic clarifications.

    Args:
        question: The exact question to ask, in the caregiver's voice.
        context: Optional dict of relevant state to surface alongside the question.
    """
    request_id, actor = _ctx(config)
    result = await _workflow.ask_caregiver(
        question, context=context, request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def flag_for_review(
    resident_id: str,
    reason: str,
    severity: str = "medium",
    config: RunnableConfig = None,
) -> dict:
    """Escalate a concern to the care-manager queue. The agent flags
    AUTONOMOUSLY when:
      - `check_vital_ranges` returned overall="abnormal" or "watch" with a
        risk on the care plan.
      - Care-plan risk + observed behaviour conflict (e.g. fall_risk + walked
        unsupervised).
      - Any new incident is recorded.

    Args:
        resident_id: The resident's UUID, as a string.
        reason: One-sentence clinical rationale.
        severity: "low" | "medium" | "high".
    """
    request_id, actor = _ctx(config)
    result = await _workflow.flag_for_review(
        _parse_uuid(resident_id), reason, FlagSeverity(severity),
        request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def schedule_followup(
    resident_id: str,
    action: str,
    when: str,
    config: RunnableConfig = None,
) -> dict:
    """Queue a concrete action for a future shift. Useful for time-bound
    rechecks (e.g. "re-measure BP at 06:00") that the next caregiver must do.

    Args:
        resident_id: The resident's UUID, as a string.
        action: Imperative one-liner ("Re-check BP", "Re-weigh", ...).
        when: ISO 8601 datetime (UTC if no timezone) for when to do it.
    """
    request_id, actor = _ctx(config)
    result = await _workflow.schedule_followup(
        _parse_uuid(resident_id), action, _parse_dt(when),
        request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def finalize_entry(
    entry_id: str,
    confirmed_by: str,
    config: RunnableConfig = None,
) -> dict:
    """Commit a draft to the permanent record. REFUSES without an explicit
    `confirmed_by` (a human signing identity); the agent must NEVER finalise
    on its own. This is the bounded-autonomy guarantee.

    Args:
        entry_id: The draft entry's UUID, as a string.
        confirmed_by: The signing caregiver's identifier (passed in from the
            UI confirmation event upstream).
    """
    request_id, actor = _ctx(config)
    result = await _workflow.finalize_entry(
        _parse_uuid(entry_id), confirmed_by=confirmed_by,
        request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def list_pending_documentation(
    shift_id: str | None = None,
    window_hours: int = 8,
    config: RunnableConfig = None,
) -> dict:
    """List residents who have NO final care event in the last `window_hours`.

    Use this at end-of-shift to answer "who haven't I documented yet?".

    Args:
        shift_id: Reserved for future use; currently ignored.
        window_hours: Rolling window in hours (default 8).
    """
    request_id, actor = _ctx(config)
    result = await _workflow.list_pending_documentation(
        shift_id, request_id=request_id, actor=actor, window_hours=window_hours,
    )
    return result.model_dump(mode="json")

ALL_TOOLS = [
    # resident
    get_resident,
    get_recent_notes,
    search_care_plan,
    check_vital_ranges,
    # drafting
    draft_sis_entry,
    validate_entry,
    synthesize_summary,
    redact_pii,
    # workflow
    ask_caregiver,
    flag_for_review,
    schedule_followup,
    finalize_entry,
    list_pending_documentation,
]


__all__ = ["ALL_TOOLS"]
