"""LangChain tool wrappers for the audited tool catalog."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.types import interrupt

from app.schemas.enums import FlagSeverity, Theme
from app.tools import drafting as _drafting
from app.tools import gaps as _gaps
from app.tools import resident as _resident
from app.tools import workflow as _workflow



def _ctx(config: RunnableConfig | None) -> tuple[str, str]:
    """Pull request_id and actor from the runtime config."""
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
    """Resolve a resident reference before any resident-specific action."""
    request_id, actor = _ctx(config)
    result = await _resident.get_resident(name_or_id, request_id=request_id, actor=actor)
    return result.model_dump(mode="json")


@tool
async def get_recent_notes(
    resident_id: str,
    days: int = 7,
    config: RunnableConfig = None,
) -> dict:
    """Fetch recent care events for a resolved resident."""
    request_id, actor = _ctx(config)
    result = await _resident.get_recent_notes(
        _parse_uuid(resident_id), days, request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def search_care_plan(resident_id: str, config: RunnableConfig = None) -> dict:
    """Fetch the resident's active care plan."""
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
    """Check vital signs before drafting a vitals entry."""
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
    """Create one structured SIS draft entry from transcript facts."""
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
    """Validate that a draft is grounded in its source transcript."""
    request_id, actor = _ctx(config)
    result = await _drafting.validate_entry(
        _parse_uuid(entry_id), source_transcript, request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


@tool
async def synthesize_summary(entry_ids: list[str], config: RunnableConfig = None) -> dict:
    """Render draft entries into a theme-organized narrative summary."""
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
    """Replace resident names with opaque tokens."""
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
    """Pause the agent and ask the caregiver a clarifying question."""
    request_id, actor = _ctx(config)
    await _workflow.ask_caregiver(
        question, context=context, request_id=request_id, actor=actor,
    )
    reply = interrupt({"question": question, "context": context or {}})
    return {"reply": reply, "question": question}


@tool
async def flag_for_review(
    resident_id: str,
    reason: str,
    severity: str = "medium",
    config: RunnableConfig = None,
) -> dict:
    """Escalate a resident concern to the care-manager queue."""
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
    """Queue a concrete follow-up action for a future shift."""
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
    """Commit a human-confirmed draft to the permanent record."""
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
    """List residents without recent finalized documentation."""
    request_id, actor = _ctx(config)
    result = await _workflow.list_pending_documentation(
        shift_id, request_id=request_id, actor=actor, window_hours=window_hours,
    )
    return result.model_dump(mode="json")

@tool
async def find_care_gaps(
    resident_id: str,
    days: int = 5,
    config: RunnableConfig = None,
) -> dict:
    """Scan a resident for unaddressed care items."""
    request_id, actor = _ctx(config)
    result = await _gaps.find_care_gaps(
        _parse_uuid(resident_id), days=days,
        request_id=request_id, actor=actor,
    )
    return result.model_dump(mode="json")


ALL_TOOLS = [
    get_resident,
    get_recent_notes,
    search_care_plan,
    check_vital_ranges,
    draft_sis_entry,
    validate_entry,
    synthesize_summary,
    redact_pii,
    ask_caregiver,
    flag_for_review,
    schedule_followup,
    finalize_entry,
    list_pending_documentation,
    find_care_gaps,
]


__all__ = ["ALL_TOOLS"]
