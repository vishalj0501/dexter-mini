"""Shared Pydantic I/O models for the tool layer.

Tool inputs and outputs are typed at this boundary because (a) the agent's
LLM sees these shapes as function signatures and (b) the audit log
serialises them. Anything that needs to round-trip through audit_log lives
here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.enums import (
    EventStatus,
    FlagSeverity,
    FollowupStatus,
    IndependenceLevel,
    Theme,
)


# ---------- resident.py ----------


class ResidentCandidate(BaseModel):
    id: UUID
    full_name: str
    room_number: str


class RecentActivity(BaseModel):
    """Lightweight summary the agent sees in its FIRST observation.

    Forces history-awareness by data flow instead of prompt exhortation:
    if `count_24h > 0`, the planner already knows there's prior activity to
    consult before drafting.
    """
    count_24h: int = 0
    last_event_at: datetime | None = None
    themes_seen_24h: list[str] = Field(default_factory=list)
    open_followups: int = 0
    open_flags: int = 0


class ResidentResolution(BaseModel):
    status: Literal["resolved", "ambiguous", "not_found"]
    resident: ResidentCandidate | None = None
    candidates: list[ResidentCandidate] = Field(default_factory=list)
    # Only populated when status == "resolved". None on ambiguous/not_found.
    recent_activity: RecentActivity | None = None


class CareEventSummary(BaseModel):
    id: UUID
    theme: Theme
    content: dict[str, Any]
    status: EventStatus
    source_transcript: str
    created_at: datetime


class RecentNotes(BaseModel):
    resident_id: UUID
    days: int
    events: list[CareEventSummary]


class CarePlanSnapshot(BaseModel):
    resident_id: UUID
    has_plan: bool
    goals: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    dietary_restrictions: str = ""
    mobility_status: str = ""


class VitalFlag(BaseModel):
    field: str
    value: float
    reason: str
    # "implausible" — value is physiologically impossible (BP > 250, HR > 250,
    # …). Agent must NEVER substitute its own value; it must ask_caregiver.
    severity: Literal["info", "warn", "critical", "implausible"]


class VitalCheckResult(BaseModel):
    resident_id: UUID
    flags: list[VitalFlag] = Field(default_factory=list)
    # "implausible" trumps everything else: at least one value is outside
    # human physiology and the agent must clarify before drafting.
    overall: Literal["normal", "watch", "abnormal", "implausible"] = "normal"


# ---------- drafting.py ----------


class DraftResult(BaseModel):
    entry_id: UUID
    theme: Theme
    parsed_content: dict[str, Any]


class FieldValidation(BaseModel):
    field: str
    confidence: float = Field(ge=0.0, le=1.0)
    grounded: bool
    note: str | None = None


class ValidationResult(BaseModel):
    entry_id: UUID
    overall_confidence: float = Field(ge=0.0, le=1.0)
    fields: list[FieldValidation] = Field(default_factory=list)
    passed: bool


class ThemeParagraph(BaseModel):
    theme: Theme
    text: str


class NarrativeSummary(BaseModel):
    entry_ids: list[UUID]
    headline: str
    paragraphs: list[ThemeParagraph] = Field(default_factory=list)


class RedactionResult(BaseModel):
    redacted_text: str
    mapping: dict[str, str] = Field(default_factory=dict)


# ---------- workflow.py ----------


class PendingQuestion(BaseModel):
    question: str
    context: dict[str, Any] = Field(default_factory=dict)
    raised_at: datetime


class FlagCreated(BaseModel):
    flag_id: UUID
    resident_id: UUID
    severity: FlagSeverity


class FollowupScheduled(BaseModel):
    followup_id: UUID
    resident_id: UUID
    action: str
    due_at: datetime
    status: FollowupStatus


class FinalizeResult(BaseModel):
    entry_id: UUID
    status: EventStatus
    finalized_at: datetime


class PendingResident(BaseModel):
    resident_id: UUID
    full_name: str
    room_number: str
    last_documented_at: datetime | None
    hours_since_last: float | None


class PendingList(BaseModel):
    window_hours: int
    pending: list[PendingResident] = Field(default_factory=list)


# Cross-cutting: independence enum re-export so tests don't reach into schemas/.
__all__ = [
    "IndependenceLevel",
    "ResidentCandidate",
    "RecentActivity",
    "ResidentResolution",
    "CareEventSummary",
    "RecentNotes",
    "CarePlanSnapshot",
    "VitalFlag",
    "VitalCheckResult",
    "DraftResult",
    "FieldValidation",
    "ValidationResult",
    "ThemeParagraph",
    "NarrativeSummary",
    "RedactionResult",
    "PendingQuestion",
    "FlagCreated",
    "FollowupScheduled",
    "FinalizeResult",
    "PendingResident",
    "PendingList",
]
