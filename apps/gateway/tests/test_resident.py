"""Tests for app/tools/resident.py."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models import AuditLog, CareEvent
from app.schemas.enums import EventStatus, Theme
from app.tools._errors import NotFoundError
from app.tools.resident import (
    check_vital_ranges,
    get_recent_notes,
    get_resident,
    search_care_plan,
)
from tests.conftest import REQUEST_ID, utcnow


# ---------- get_resident ----------


async def test_get_resident_resolves_by_exact_surname(resident):
    result = await get_resident("Müller", request_id=REQUEST_ID)
    assert result.status == "resolved"
    assert result.resident is not None
    assert result.resident.id == resident.id


async def test_get_resident_strips_honorific(resident):
    result = await get_resident("Frau Müller", request_id=REQUEST_ID)
    assert result.status == "resolved"


async def test_get_resident_room_lookup(resident):
    result = await get_resident("12", request_id=REQUEST_ID)
    assert result.status == "resolved"
    assert result.resident.room_number == "12"


async def test_get_resident_ambiguous(resident, other_resident):
    result = await get_resident("Müller", request_id=REQUEST_ID)
    assert result.status == "ambiguous"
    assert {c.id for c in result.candidates} == {resident.id, other_resident.id}


async def test_get_resident_not_found(resident):
    result = await get_resident("Schwarzenegger", request_id=REQUEST_ID)
    assert result.status == "not_found"


async def test_get_resident_uuid_fast_path(resident):
    result = await get_resident(str(resident.id), request_id=REQUEST_ID)
    assert result.status == "resolved"
    assert result.resident.id == resident.id


async def test_get_resident_writes_audit(resident):
    await get_resident("Müller", request_id=REQUEST_ID)
    rows = await AuditLog.all()
    assert len(rows) == 1
    assert rows[0].action == "tool.get_resident"
    assert rows[0].request_id == REQUEST_ID
    assert rows[0].payload["status"] == "ok"


# ---------- get_recent_notes ----------


async def test_get_recent_notes_orders_newest_first(resident):
    older = utcnow() - timedelta(days=2)
    newer = utcnow() - timedelta(hours=1)
    await CareEvent.create(
        resident=resident, theme=Theme.VITALS, content={"bp_systolic": 130},
        source_transcript="", status=EventStatus.FINAL, created_at=older,
    )
    await CareEvent.create(
        resident=resident, theme=Theme.NUTRITION, content={"appetite": "good"},
        source_transcript="", status=EventStatus.FINAL, created_at=newer,
    )

    result = await get_recent_notes(resident.id, days=7, request_id=REQUEST_ID)
    assert len(result.events) == 2
    assert result.events[0].theme == Theme.NUTRITION  # newest first


async def test_get_recent_notes_window_excludes_old(resident):
    await CareEvent.create(
        resident=resident, theme=Theme.VITALS, content={},
        source_transcript="", status=EventStatus.FINAL,
        created_at=utcnow() - timedelta(days=30),
    )
    result = await get_recent_notes(resident.id, days=7, request_id=REQUEST_ID)
    assert result.events == []


async def test_get_recent_notes_unknown_resident_raises():
    with pytest.raises(NotFoundError):
        await get_recent_notes(uuid.uuid4(), request_id=REQUEST_ID)


# ---------- search_care_plan ----------


async def test_search_care_plan_returns_active_plan(resident):
    snap = await search_care_plan(resident.id, request_id=REQUEST_ID)
    assert snap.has_plan is True
    assert "fall_risk" in snap.risk_flags
    assert "Low salt" in snap.dietary_restrictions


async def test_search_care_plan_no_plan(other_resident):
    # other_resident has an empty plan (still active); has_plan = True
    snap = await search_care_plan(other_resident.id, request_id=REQUEST_ID)
    assert snap.has_plan is True
    assert snap.goals == []


async def test_search_care_plan_unknown_resident():
    with pytest.raises(NotFoundError):
        await search_care_plan(uuid.uuid4(), request_id=REQUEST_ID)


# ---------- check_vital_ranges ----------


async def test_check_vital_ranges_normal(resident):
    result = await check_vital_ranges(
        resident.id,
        vitals={"bp_systolic": 128, "bp_diastolic": 80, "heart_rate": 72},
        request_id=REQUEST_ID,
    )
    assert result.overall == "normal"
    assert result.flags == []


async def test_check_vital_ranges_warns_elevated_bp(resident):
    result = await check_vital_ranges(
        resident.id,
        vitals={"bp_systolic": 170, "bp_diastolic": 95},
        request_id=REQUEST_ID,
    )
    assert result.overall == "watch"
    fields = {f.field for f in result.flags}
    assert "bp_systolic" in fields


async def test_check_vital_ranges_critical_o2(resident):
    result = await check_vital_ranges(
        resident.id,
        vitals={"o2_sat": 84},
        request_id=REQUEST_ID,
    )
    assert result.overall == "abnormal"
    assert any(f.severity == "critical" for f in result.flags)


async def test_check_vital_ranges_baseline_delta(resident):
    # baseline systolic is 130 → 165 is 27% delta → triggers baseline-delta flag,
    # and 165 is also above warn band → so we get a warn flag.
    result = await check_vital_ranges(
        resident.id,
        vitals={"bp_systolic": 165},
        request_id=REQUEST_ID,
    )
    assert result.overall in {"watch", "abnormal"}
    assert any("differs from baseline" in f.reason or "above normal" in f.reason for f in result.flags)
