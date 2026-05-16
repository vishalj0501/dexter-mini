"""Tests for app/tools/workflow.py."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models import CareEvent, Followup, ReviewFlag
from app.schemas.enums import EventStatus, FlagSeverity, FollowupStatus, Theme
from app.tools._errors import InvalidStateError, NotFoundError
from app.tools.workflow import (
    ask_caregiver,
    finalize_entry,
    flag_for_review,
    list_pending_documentation,
    schedule_followup,
)
from tests.conftest import REQUEST_ID, utcnow


# ---------- ask_caregiver ----------


async def test_ask_caregiver_returns_question():
    result = await ask_caregiver(
        "Did you measure BP today?",
        context={"reason": "yesterday elevated"},
        request_id=REQUEST_ID,
    )
    assert result.question == "Did you measure BP today?"
    assert result.context["reason"] == "yesterday elevated"
    assert result.raised_at is not None


async def test_ask_caregiver_empty_rejected():
    with pytest.raises(InvalidStateError):
        await ask_caregiver("   ", request_id=REQUEST_ID)


# ---------- flag_for_review ----------


async def test_flag_for_review_creates_row(resident):
    result = await flag_for_review(
        resident.id,
        "Out-of-range BP",
        severity=FlagSeverity.HIGH,
        request_id=REQUEST_ID,
    )
    row = await ReviewFlag.get(id=result.flag_id)
    assert row.severity == FlagSeverity.HIGH
    assert row.reason == "Out-of-range BP"
    assert row.resolved is False


async def test_flag_for_review_empty_reason(resident):
    with pytest.raises(InvalidStateError):
        await flag_for_review(resident.id, "", request_id=REQUEST_ID)


async def test_flag_for_review_unknown_resident():
    with pytest.raises(NotFoundError):
        await flag_for_review(uuid.uuid4(), "reason", request_id=REQUEST_ID)


# ---------- schedule_followup ----------


async def test_schedule_followup_creates_row(resident):
    due = utcnow() + timedelta(hours=6)
    result = await schedule_followup(
        resident.id,
        "Re-check BP at 06:00",
        when=due,
        request_id=REQUEST_ID,
    )
    row = await Followup.get(id=result.followup_id)
    assert row.action == "Re-check BP at 06:00"
    assert row.status == FollowupStatus.OPEN


async def test_schedule_followup_empty_action(resident):
    with pytest.raises(InvalidStateError):
        await schedule_followup(
            resident.id, "  ", when=utcnow(), request_id=REQUEST_ID,
        )


async def test_schedule_followup_unknown_resident():
    with pytest.raises(NotFoundError):
        await schedule_followup(
            uuid.uuid4(), "action", when=utcnow(), request_id=REQUEST_ID,
        )


# ---------- finalize_entry ----------


async def test_finalize_entry_transitions_draft_to_final(resident):
    draft = await CareEvent.create(
        resident=resident,
        theme=Theme.VITALS,
        content={"bp_systolic": 120},
        source_transcript="BP 120 over 80",
        status=EventStatus.DRAFT,
    )
    result = await finalize_entry(
        draft.id,
        confirmed_by="nurse-anna",
        request_id=REQUEST_ID,
    )
    assert result.status == EventStatus.FINAL
    after = await CareEvent.get(id=draft.id)
    assert after.status == EventStatus.FINAL
    assert after.finalized_at is not None
    assert after.created_by == "nurse-anna"


async def test_finalize_entry_requires_confirmed_by(resident):
    draft = await CareEvent.create(
        resident=resident, theme=Theme.VITALS, content={},
        source_transcript="", status=EventStatus.DRAFT,
    )
    with pytest.raises(InvalidStateError):
        await finalize_entry(draft.id, confirmed_by="", request_id=REQUEST_ID)


async def test_finalize_entry_refuses_double_finalize(resident):
    draft = await CareEvent.create(
        resident=resident, theme=Theme.VITALS, content={},
        source_transcript="", status=EventStatus.DRAFT,
    )
    await finalize_entry(draft.id, confirmed_by="nurse-x", request_id=REQUEST_ID)
    with pytest.raises(InvalidStateError):
        await finalize_entry(draft.id, confirmed_by="nurse-x", request_id=REQUEST_ID)


async def test_finalize_entry_unknown():
    with pytest.raises(NotFoundError):
        await finalize_entry(uuid.uuid4(), confirmed_by="x", request_id=REQUEST_ID)


# ---------- list_pending_documentation ----------


async def test_pending_documentation_lists_undocumented(resident, other_resident):
    # resident has a recent FINAL event; other_resident has none.
    await CareEvent.create(
        resident=resident, theme=Theme.VITALS, content={},
        source_transcript="", status=EventStatus.FINAL,
        created_at=utcnow() - timedelta(hours=1),
    )
    result = await list_pending_documentation(window_hours=8, request_id=REQUEST_ID)
    ids = {p.resident_id for p in result.pending}
    assert other_resident.id in ids
    assert resident.id not in ids


async def test_pending_documentation_includes_stale(resident):
    # An old event outside the window → resident is pending again.
    await CareEvent.create(
        resident=resident, theme=Theme.VITALS, content={},
        source_transcript="", status=EventStatus.FINAL,
        created_at=utcnow() - timedelta(hours=24),
    )
    result = await list_pending_documentation(window_hours=8, request_id=REQUEST_ID)
    assert any(p.resident_id == resident.id for p in result.pending)
    p = next(p for p in result.pending if p.resident_id == resident.id)
    assert p.hours_since_last is not None and p.hours_since_last > 8


async def test_pending_documentation_draft_does_not_count(resident):
    # A DRAFT event does not satisfy "documented" — final is the bar.
    await CareEvent.create(
        resident=resident, theme=Theme.VITALS, content={},
        source_transcript="", status=EventStatus.DRAFT,
        created_at=utcnow() - timedelta(minutes=10),
    )
    result = await list_pending_documentation(window_hours=8, request_id=REQUEST_ID)
    assert any(p.resident_id == resident.id for p in result.pending)
