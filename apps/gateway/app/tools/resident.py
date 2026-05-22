"""Read-only resident lookup and context tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from tortoise.expressions import Q

from app.models import CareEvent, CarePlan, Followup, Resident, ReviewFlag
from app.schemas.enums import AuditAction, FollowupStatus, IndependenceLevel
from app.tools._audit import audited
from app.tools._errors import NotFoundError
from app.tools._types import (
    CareEventSummary,
    CarePlanSnapshot,
    RecentActivity,
    RecentNotes,
    ResidentCandidate,
    ResidentResolution,
    VitalCheckResult,
    VitalFlag,
)


def _candidate(r: Resident) -> ResidentCandidate:
    return ResidentCandidate(id=r.id, full_name=r.full_name, room_number=r.room_number)


async def _recent_activity(resident_id: UUID) -> RecentActivity:
    """Build a 24h activity snapshot."""
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    events = (
        await CareEvent.filter(resident_id=resident_id, created_at__gte=since)
        .order_by("-created_at")
        .values("theme", "created_at")
    )
    open_followups = await Followup.filter(
        resident_id=resident_id, status=FollowupStatus.OPEN
    ).count()
    open_flags = await ReviewFlag.filter(resident_id=resident_id, resolved=False).count()

    if not events:
        return RecentActivity(
            count_24h=0,
            open_followups=open_followups,
            open_flags=open_flags,
        )

    themes: list[str] = []
    for row in events:
        theme = row["theme"]
        theme_str = theme.value if hasattr(theme, "value") else str(theme)
        if theme_str not in themes:
            themes.append(theme_str)

    return RecentActivity(
        count_24h=len(events),
        last_event_at=events[0]["created_at"],
        themes_seen_24h=themes,
        open_followups=open_followups,
        open_flags=open_flags,
    )


@audited(AuditAction.GET_RESIDENT)
async def get_resident(
    name_or_id: str,
    *,
    request_id: str,
    actor: str = "agent",
) -> ResidentResolution:
    """Resolve a free-form reference to a resident."""
    needle = name_or_id.strip()
    if not needle:
        return ResidentResolution(status="not_found")

    try:
        rid = UUID(needle)
    except ValueError:
        rid = None

    if rid is not None:
        resident = await Resident.get_or_none(id=rid)
        if resident is None:
            return ResidentResolution(status="not_found")
        return ResidentResolution(
            status="resolved",
            resident=_candidate(resident),
            recent_activity=await _recent_activity(resident.id),
        )

    cleaned = needle
    for prefix in ("frau ", "fr. ", "fr ", "herr ", "hr. ", "hr ", "mrs. ", "mrs ", "mr. ", "mr "):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break

    matches = await Resident.filter(
        Q(first_name__icontains=cleaned)
        | Q(last_name__icontains=cleaned)
        | Q(room_number__iexact=cleaned)
    ).all()

    if not matches:
        return ResidentResolution(status="not_found")
    if len(matches) == 1:
        return ResidentResolution(
            status="resolved",
            resident=_candidate(matches[0]),
            recent_activity=await _recent_activity(matches[0].id),
        )
    return ResidentResolution(
        status="ambiguous",
        candidates=[_candidate(r) for r in matches],
    )


@audited(AuditAction.GET_RECENT_NOTES)
async def get_recent_notes(
    resident_id: UUID,
    days: int = 7,
    *,
    request_id: str,
    actor: str = "agent",
) -> RecentNotes:
    """Fetch recent care events for context."""
    if days < 1:
        days = 1
    since = datetime.now(timezone.utc) - timedelta(days=days)

    resident = await Resident.get_or_none(id=resident_id)
    if resident is None:
        raise NotFoundError(f"resident {resident_id} not found")

    rows = (
        await CareEvent.filter(resident_id=resident_id, created_at__gte=since)
        .order_by("-created_at")
        .all()
    )
    events = [
        CareEventSummary(
            id=row.id,
            theme=row.theme,
            content=row.content,
            status=row.status,
            source_transcript=row.source_transcript,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return RecentNotes(resident_id=resident_id, days=days, events=events)


@audited(AuditAction.SEARCH_CARE_PLAN)
async def search_care_plan(
    resident_id: UUID,
    *,
    request_id: str,
    actor: str = "agent",
) -> CarePlanSnapshot:
    """Active care plan: goals, risk flags, dietary restrictions, mobility status."""
    resident = await Resident.get_or_none(id=resident_id)
    if resident is None:
        raise NotFoundError(f"resident {resident_id} not found")

    plan = await CarePlan.filter(resident_id=resident_id, active=True).first()
    if plan is None:
        return CarePlanSnapshot(resident_id=resident_id, has_plan=False)
    return CarePlanSnapshot(
        resident_id=resident_id,
        has_plan=True,
        goals=list(plan.goals or []),
        risk_flags=list(plan.risk_flags or []),
        dietary_restrictions=plan.dietary_restrictions or "",
        mobility_status=plan.mobility_status or "",
    )


_VITAL_RULES: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "bp_systolic": (90, 100, 160, 180),
    "bp_diastolic": (60, 65, 100, 110),
    "heart_rate": (50, 55, 100, 120),
    "temperature_c": (35.0, 35.5, 37.8, 38.5),
    "o2_sat": (88, 92, 100, 100),
}


_PLAUSIBLE_BOUNDS: dict[str, tuple[float, float]] = {
    "bp_systolic": (40, 260),
    "bp_diastolic": (20, 180),
    "heart_rate": (20, 250),
    "temperature_c": (28.0, 43.0),
    "o2_sat": (40, 100),
}


def _implausible(field: str, value: float) -> VitalFlag | None:
    lo, hi = _PLAUSIBLE_BOUNDS.get(field, (float("-inf"), float("inf")))
    if value < lo or value > hi:
        return VitalFlag(
            field=field,
            value=value,
            reason=(
                f"{field} {value} is physiologically impossible "
                f"(expected {lo}–{hi}). Likely transcription error."
            ),
            severity="implausible",
        )
    return None


def _classify(field: str, value: float) -> VitalFlag | None:
    rule = _VITAL_RULES.get(field)
    if rule is None:
        return None
    crit_lo, warn_lo, warn_hi, crit_hi = rule
    if value < crit_lo:
        return VitalFlag(field=field, value=value, reason=f"{field} {value} below critical {crit_lo}", severity="critical")
    if value > crit_hi:
        return VitalFlag(field=field, value=value, reason=f"{field} {value} above critical {crit_hi}", severity="critical")
    if value < warn_lo:
        return VitalFlag(field=field, value=value, reason=f"{field} {value} below normal {warn_lo}", severity="warn")
    if value > warn_hi:
        return VitalFlag(field=field, value=value, reason=f"{field} {value} above normal {warn_hi}", severity="warn")
    return None


def _baseline_delta_flags(baseline: dict, vitals: dict) -> list[VitalFlag]:
    flags: list[VitalFlag] = []
    for field in ("bp_systolic", "bp_diastolic", "heart_rate"):
        base = baseline.get(field)
        cur = vitals.get(field)
        if base is None or cur is None or base == 0:
            continue
        delta = abs(cur - base) / base
        if delta >= 0.20:
            flags.append(
                VitalFlag(
                    field=field,
                    value=float(cur),
                    reason=f"{field} {cur} differs from baseline {base} by {int(delta*100)}%",
                    severity="warn",
                )
            )
    return flags


@audited(AuditAction.CHECK_VITAL_RANGES)
async def check_vital_ranges(
    resident_id: UUID,
    vitals: dict[str, Any],
    *,
    request_id: str,
    actor: str = "agent",
) -> VitalCheckResult:
    """Sanity-check vitals against clinical bands + resident baseline."""
    resident = await Resident.get_or_none(id=resident_id)
    if resident is None:
        raise NotFoundError(f"resident {resident_id} not found")

    flags: list[VitalFlag] = []
    implausible_flags: list[VitalFlag] = []
    for field, value in vitals.items():
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        imp = _implausible(field, numeric)
        if imp:
            implausible_flags.append(imp)
            continue
        flag = _classify(field, numeric)
        if flag:
            flags.append(flag)

    if implausible_flags:
        return VitalCheckResult(
            resident_id=resident_id,
            flags=implausible_flags,
            overall="implausible",
        )

    flags.extend(_baseline_delta_flags(resident.baseline_vitals or {}, vitals))

    if any(f.severity == "critical" for f in flags):
        overall = "abnormal"
    elif flags:
        overall = "watch"
    else:
        overall = "normal"

    return VitalCheckResult(resident_id=resident_id, flags=flags, overall=overall)


__all__ = [
    "IndependenceLevel",
    "get_resident",
    "get_recent_notes",
    "search_care_plan",
    "check_vital_ranges",
]
