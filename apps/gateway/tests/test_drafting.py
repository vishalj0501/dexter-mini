"""Tests for app/tools/drafting.py."""

from __future__ import annotations

import uuid

import pytest

from app.models import CareEvent
from app.schemas.enums import EventStatus, Theme
from app.tools._errors import NotFoundError, SchemaError
from app.tools.drafting import (
    draft_sis_entry,
    redact_pii,
    synthesize_summary,
    validate_entry,
)
from tests.conftest import REQUEST_ID


# ---------- draft_sis_entry ----------


async def test_draft_sis_entry_creates_draft(resident):
    result = await draft_sis_entry(
        Theme.VITALS,
        resident.id,
        {"bp_systolic": 120, "bp_diastolic": 80, "heart_rate": 72},
        "BP 120 over 80, pulse 72.",
        request_id=REQUEST_ID,
    )
    assert result.theme == Theme.VITALS
    entry = await CareEvent.get(id=result.entry_id)
    assert entry.status == EventStatus.DRAFT
    assert entry.request_id == REQUEST_ID
    assert entry.content["bp_systolic"] == 120


async def test_draft_sis_entry_rejects_bad_schema(resident):
    with pytest.raises(SchemaError):
        await draft_sis_entry(
            Theme.VITALS,
            resident.id,
            {"bp_systolic": 999},  # exceeds 260 ceiling
            "",
            request_id=REQUEST_ID,
        )


async def test_draft_sis_entry_unknown_resident():
    with pytest.raises(NotFoundError):
        await draft_sis_entry(
            Theme.VITALS, uuid.uuid4(), {"bp_systolic": 120}, "",
            request_id=REQUEST_ID,
        )


# ---------- validate_entry ----------


async def test_validate_entry_grounded_passes(resident):
    drafted = await draft_sis_entry(
        Theme.VITALS,
        resident.id,
        {"bp_systolic": 120, "bp_diastolic": 80, "heart_rate": 72},
        "Her blood pressure is 120 over 80 and her pulse is 72.",
        request_id=REQUEST_ID,
    )
    result = await validate_entry(
        drafted.entry_id,
        "Her blood pressure is 120 over 80 and her pulse is 72.",
        request_id=REQUEST_ID,
    )
    assert result.passed is True
    assert result.overall_confidence >= 0.9


async def test_validate_entry_ungrounded_fails_and_flips_status(resident):
    drafted = await draft_sis_entry(
        Theme.VITALS,
        resident.id,
        {"bp_systolic": 200, "bp_diastolic": 110, "heart_rate": 130},
        "She slept well.",  # no numbers
        request_id=REQUEST_ID,
    )
    result = await validate_entry(
        drafted.entry_id,
        "She slept well.",
        request_id=REQUEST_ID,
    )
    assert result.passed is False
    # status should flip to NEEDS_REVIEW
    entry = await CareEvent.get(id=drafted.entry_id)
    assert entry.status == EventStatus.NEEDS_REVIEW
    assert entry.validator_confidence is not None


async def test_validate_entry_unknown():
    with pytest.raises(NotFoundError):
        await validate_entry(uuid.uuid4(), "", request_id=REQUEST_ID)


# ---------- synthesize_summary ----------


async def test_synthesize_summary_combines_themes(resident):
    vitals = await draft_sis_entry(
        Theme.VITALS, resident.id,
        {"bp_systolic": 120, "bp_diastolic": 80, "heart_rate": 72},
        "BP 120/80, pulse 72.",
        request_id=REQUEST_ID,
    )
    nutrition = await draft_sis_entry(
        Theme.NUTRITION, resident.id,
        {"appetite": "reduced", "meals": [{"meal": "breakfast", "intake_pct": 30, "refused": False}]},
        "Ate only a third of breakfast.",
        request_id=REQUEST_ID,
    )

    summary = await synthesize_summary([vitals.entry_id, nutrition.entry_id], request_id=REQUEST_ID)
    assert len(summary.paragraphs) == 2
    themes = {p.theme for p in summary.paragraphs}
    assert themes == {Theme.VITALS, Theme.NUTRITION}
    assert "BP 120/80" in next(p.text for p in summary.paragraphs if p.theme == Theme.VITALS)


async def test_synthesize_summary_empty():
    summary = await synthesize_summary([], request_id=REQUEST_ID)
    assert summary.headline == "No entries to summarise."
    assert summary.paragraphs == []


# ---------- redact_pii ----------


async def test_redact_pii_replaces_known_names(resident):
    result = await redact_pii(
        "Margarethe seemed tired today; Mrs. Müller refused breakfast.",
        request_id=REQUEST_ID,
    )
    assert "Margarethe" not in result.redacted_text
    assert "Müller" not in result.redacted_text
    assert result.mapping  # at least one token


async def test_redact_pii_with_extra_names():
    result = await redact_pii(
        "Dr. Schmidt visited today.",
        extra_names=["Schmidt"],
        request_id=REQUEST_ID,
    )
    assert "Schmidt" not in result.redacted_text


async def test_redact_pii_empty_text():
    result = await redact_pii("", request_id=REQUEST_ID)
    assert result.redacted_text == ""
    assert result.mapping == {}
