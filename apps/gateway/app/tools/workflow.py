"""Workflow tools — agent actions that change the world.

These are the tools that turn the agent from a transcriber into a participant:
asking the caregiver a clarifying question, raising a flag for the care
manager, scheduling a follow-up, finalising an entry the caregiver confirmed,
and surfacing what's still missing this shift.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from tortoise.functions import Max

from app.models import CareEvent, Followup, Resident, ReviewFlag
from app.schemas.enums import (
    AuditAction,
    EventStatus,
    FlagSeverity,
    FollowupStatus,
)
from app.tools._audit import audited
from app.tools._errors import InvalidStateError, NotFoundError
from app.tools._types import (
    FinalizeResult,
    FlagCreated,
    FollowupScheduled,
    PendingList,
    PendingQuestion,
    PendingResident,
)


@audited(AuditAction.ASK_CAREGIVER)
async def ask_caregiver(
    question: str,
    *,
    request_id: str,
    actor: str = "agent",
    context: dict[str, Any] | None = None,
) -> PendingQuestion:
    """Surface a clarifying question.

    Day-2 contract: return a `PendingQuestion` and record the ask in the audit
    log. Day 4 wraps this call site with LangGraph's `interrupt()` so the loop
    pauses for the caregiver's answer; the tool body stays the same.
    """
    question = question.strip()
    if not question:
        raise InvalidStateError("ask_caregiver requires a non-empty question")
    return PendingQuestion(
        question=question,
        context=context or {},
        raised_at=datetime.now(timezone.utc),
    )


@audited(AuditAction.FLAG_FOR_REVIEW)
async def flag_for_review(
    resident_id: UUID,
    reason: str,
    severity: FlagSeverity = FlagSeverity.MEDIUM,
    *,
    request_id: str,
    actor: str = "agent",
) -> FlagCreated:
    """Raise a review flag for care-manager attention."""
    reason = reason.strip()
    if not reason:
        raise InvalidStateError("flag_for_review requires a non-empty reason")
    resident = await Resident.get_or_none(id=resident_id)
    if resident is None:
        raise NotFoundError(f"resident {resident_id} not found")

    flag = await ReviewFlag.create(
        resident=resident,
        reason=reason,
        severity=severity,
        raised_by=actor,
        request_id=request_id,
    )
    return FlagCreated(flag_id=flag.id, resident_id=resident_id, severity=severity)


@audited(AuditAction.SCHEDULE_FOLLOWUP)
async def schedule_followup(
    resident_id: UUID,
    action: str,
    when: datetime,
    *,
    request_id: str,
    actor: str = "agent",
) -> FollowupScheduled:
    """Queue a concrete follow-up action for a future shift."""
    action = action.strip()
    if not action:
        raise InvalidStateError("schedule_followup requires a non-empty action")
    resident = await Resident.get_or_none(id=resident_id)
    if resident is None:
        raise NotFoundError(f"resident {resident_id} not found")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    row = await Followup.create(
        resident=resident,
        action=action,
        due_at=when,
        raised_by=actor,
        request_id=request_id,
    )
    return FollowupScheduled(
        followup_id=row.id,
        resident_id=resident_id,
        action=action,
        due_at=row.due_at,
        status=row.status,
    )


@audited(AuditAction.FINALIZE_ENTRY)
async def finalize_entry(
    entry_id: UUID,
    *,
    confirmed_by: str,
    request_id: str,
    actor: str = "agent",
) -> FinalizeResult:
    """Commit a draft entry to the permanent record.

    Requires `confirmed_by` — the agent cannot finalise on its own; the upstream
    request must carry the signing nurse's identity. This is the bounded-autonomy
    contract enforced at the tool layer.
    """
    if not confirmed_by or not confirmed_by.strip():
        raise InvalidStateError("finalize_entry requires confirmed_by (signing identity)")
    entry = await CareEvent.get_or_none(id=entry_id)
    if entry is None:
        raise NotFoundError(f"entry {entry_id} not found")
    if entry.status == EventStatus.FINAL:
        raise InvalidStateError(f"entry {entry_id} is already final")

    now = datetime.now(timezone.utc)
    entry.status = EventStatus.FINAL
    entry.finalized_at = now
    entry.created_by = confirmed_by.strip()
    await entry.save(update_fields=["status", "finalized_at", "created_by"])

    return FinalizeResult(entry_id=entry.id, status=entry.status, finalized_at=now)


@audited(AuditAction.LIST_PENDING_DOCUMENTATION)
async def list_pending_documentation(
    shift_id: str | None = None,
    *,
    request_id: str,
    actor: str = "agent",
    window_hours: int = 8,
) -> PendingList:
    """Residents who have no final care event in the last `window_hours`.

    `shift_id` is accepted for forward compatibility (a Shift table lands in
    Day 6 when the time-blocked task list ships); for now we use a rolling
    window which matches the way the agent reasons about "this shift."
    """
    if window_hours < 1:
        window_hours = 1
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    # last final event per resident, in one query
    rows = (
        await CareEvent.filter(status=EventStatus.FINAL)
        .annotate(last_final=Max("created_at"))
        .group_by("resident_id")
        .values("resident_id", "last_final")
    )
    last_seen: dict[UUID, datetime] = {r["resident_id"]: r["last_final"] for r in rows}

    pending: list[PendingResident] = []
    residents = await Resident.all()
    now = datetime.now(timezone.utc)
    for r in residents:
        last = last_seen.get(r.id)
        if last is None or last < cutoff:
            hours = (now - last).total_seconds() / 3600 if last else None
            pending.append(
                PendingResident(
                    resident_id=r.id,
                    full_name=r.full_name,
                    room_number=r.room_number,
                    last_documented_at=last,
                    hours_since_last=hours,
                )
            )
    pending.sort(key=lambda p: (p.hours_since_last is None, -(p.hours_since_last or 0.0)))
    return PendingList(window_hours=window_hours, pending=pending)


__all__ = [
    "ask_caregiver",
    "flag_for_review",
    "schedule_followup",
    "finalize_entry",
    "list_pending_documentation",
]
