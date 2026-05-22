"""Care-gap detection tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.models import CareEvent, CarePlan, Followup, Resident, ReviewFlag
from app.schemas.enums import (
    AuditAction,
    EventStatus,
    FollowupStatus,
    Theme,
)
from app.tools._audit import audited
from app.tools._errors import NotFoundError
from app.tools._types import CareGap, CareGapReport


_RISK_THEME_MAP: dict[str, tuple[Theme, list[str]]] = {
    "fall_risk":       (Theme.MOBILITY,  ["walk", "walked", "alone", "unaccompanied", "supervis", "fell", "fall"]),
    "wandering_risk":  (Theme.MOBILITY,  ["walk", "corridor", "hallway", "alone", "lost"]),
    "hypertension":    (Theme.VITALS,    ["bp", "blood pressure", "systolic"]),
    "chf":             (Theme.VITALS,    ["bp", "weight", "swelling", "edema"]),
    "diabetes_type2":  (Theme.NUTRITION, ["sugar", "carb", "sweet", "ate"]),
    "dementia":        (Theme.COGNITION, ["confus", "orient", "wander"]),
    "pressure_ulcer_risk": (Theme.MOBILITY, ["bedridden", "repositioned", "sore"]),
}


def _theme_str(theme) -> str:
    return theme.value if hasattr(theme, "value") else str(theme)


async def _nutrition_pattern(resident_id: UUID, since: datetime) -> CareGap | None:
    """Find repeated meal refusals in the window."""
    events = await CareEvent.filter(
        resident_id=resident_id,
        theme=Theme.NUTRITION,
        created_at__gte=since,
    ).order_by("-created_at").all()
    if not events:
        return None

    refusals: list[dict] = []
    for ev in events:
        content = ev.content or {}
        meals = content.get("meals") or []
        for meal in meals:
            if isinstance(meal, dict) and meal.get("refused"):
                refusals.append({
                    "entry_id": str(ev.id),
                    "meal": meal.get("meal", "?"),
                    "at": ev.created_at.isoformat(),
                })
        if str(content.get("appetite", "")).lower() in {"refused", "poor"}:
            refusals.append({
                "entry_id": str(ev.id),
                "meal": str(content.get("appetite")),
                "at": ev.created_at.isoformat(),
            })

    if len(refusals) >= 3:
        return CareGap(
            kind="nutrition_pattern",
            severity="watch",
            description=(
                f"{len(refusals)} meal refusals in the past "
                f"{(datetime.now(timezone.utc) - since).days} days. "
                "Worth checking appetite, nausea, swallowing, or mood."
            ),
            evidence={"refusals": refusals[:5]},
            suggested_action="schedule_followup: nutrition / appetite review",
        )
    return None


async def _missing_vital_today(resident_id: UUID, baseline: dict) -> CareGap | None:
    """If vitals were elevated yesterday but not measured today, that's a gap."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)

    today_vitals = await CareEvent.filter(
        resident_id=resident_id, theme=Theme.VITALS, created_at__gte=today_start,
    ).count()
    if today_vitals > 0:
        return None

    yesterday_vitals = await CareEvent.filter(
        resident_id=resident_id, theme=Theme.VITALS,
        created_at__gte=yesterday_start, created_at__lt=today_start,
    ).order_by("-created_at").all()
    if not yesterday_vitals:
        return None

    elevated_evidence: list[dict] = []
    for ev in yesterday_vitals:
        content = ev.content or {}
        sys_bp = content.get("bp_systolic")
        dia_bp = content.get("bp_diastolic")
        if isinstance(sys_bp, (int, float)) and sys_bp >= 140:
            elevated_evidence.append({"bp_systolic": sys_bp, "at": ev.created_at.isoformat()})
        if isinstance(dia_bp, (int, float)) and dia_bp >= 90:
            elevated_evidence.append({"bp_diastolic": dia_bp, "at": ev.created_at.isoformat()})

    if not elevated_evidence:
        return None

    return CareGap(
        kind="missing_vital",
        severity="high",
        description=(
            "Yesterday's BP was elevated and no vitals have been recorded today. "
            "Ask the caregiver to measure before finalising the shift."
        ),
        evidence={"yesterday": elevated_evidence[:3]},
        suggested_action="ask_caregiver: today's BP reading",
    )


async def _escalating_vitals(resident_id: UUID, since: datetime) -> CareGap | None:
    """3+ systolic readings in the window trending upward (each >= prior by 5+)."""
    events = await CareEvent.filter(
        resident_id=resident_id, theme=Theme.VITALS, created_at__gte=since,
    ).order_by("created_at").all()
    readings: list[tuple[float, datetime]] = []
    for ev in events:
        v = (ev.content or {}).get("bp_systolic")
        if isinstance(v, (int, float)) and v > 0:
            readings.append((float(v), ev.created_at))
    if len(readings) < 3:
        return None
    last3 = readings[-3:]
    if last3[0][0] + 5 <= last3[1][0] <= last3[2][0] - 5:
        return CareGap(
            kind="escalating_vital",
            severity="high",
            description=(
                f"Three consecutive systolic readings trending up: "
                f"{int(last3[0][0])} → {int(last3[1][0])} → {int(last3[2][0])}."
            ),
            evidence={"readings": [{"sys": int(v), "at": t.isoformat()} for v, t in last3]},
            suggested_action="flag_for_review: escalating BP, high severity",
        )
    return None


async def _plan_risk_unaddressed(
    resident_id: UUID, since: datetime, plan: CarePlan | None,
) -> list[CareGap]:
    """Find plan risks touched by today's events without a review flag."""
    if plan is None or not plan.risk_flags:
        return []
    today_events = await CareEvent.filter(
        resident_id=resident_id, created_at__gte=since,
    ).all()
    if not today_events:
        return []
    open_flags = await ReviewFlag.filter(
        resident_id=resident_id, resolved=False, created_at__gte=since,
    ).count()

    gaps: list[CareGap] = []
    for risk in plan.risk_flags:
        mapping = _RISK_THEME_MAP.get(risk.lower())
        if mapping is None:
            continue
        theme, keywords = mapping
        related: list[dict] = []
        for ev in today_events:
            if _theme_str(ev.theme) != _theme_str(theme):
                continue
            haystack = (ev.source_transcript or "") + " " + str(ev.content or "")
            if any(k.lower() in haystack.lower() for k in keywords):
                related.append({"entry_id": str(ev.id), "theme": _theme_str(ev.theme)})
        if related and open_flags == 0:
            gaps.append(CareGap(
                kind="plan_risk_unaddressed",
                severity="watch",
                description=(
                    f"Care plan flags '{risk}', and today's events touched the related "
                    f"area ({_theme_str(theme)}), but no review flag has been raised."
                ),
                evidence={"risk": risk, "related_events": related[:3]},
                suggested_action=f"flag_for_review: {risk} context not addressed",
            ))
    return gaps


async def _overdue_followups(resident_id: UUID) -> list[CareGap]:
    now = datetime.now(timezone.utc)
    rows = await Followup.filter(
        resident_id=resident_id, status=FollowupStatus.OPEN, due_at__lt=now,
    ).order_by("due_at").all()
    if not rows:
        return []
    return [CareGap(
        kind="overdue_followup",
        severity="high" if len(rows) > 1 else "watch",
        description=(
            f"{len(rows)} open follow-up{'s' if len(rows) > 1 else ''} past due. "
            "Address or reschedule before end of shift."
        ),
        evidence={"items": [
            {"id": str(f.id), "action": f.action, "due_at": f.due_at.isoformat()}
            for f in rows[:5]
        ]},
        suggested_action="resolve or reschedule each follow-up",
    )]


@audited(AuditAction.FIND_CARE_GAPS)
async def find_care_gaps(
    resident_id: UUID,
    days: int = 5,
    *,
    request_id: str,
    actor: str = "agent",
) -> CareGapReport:
    """Scan recent resident history for unaddressed care items."""
    resident = await Resident.get_or_none(id=resident_id)
    if resident is None:
        raise NotFoundError(f"resident {resident_id} not found")

    since = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    plan = await CarePlan.filter(resident_id=resident_id, active=True).first()

    gaps: list[CareGap] = []
    if g := await _nutrition_pattern(resident_id, since):
        gaps.append(g)
    if g := await _missing_vital_today(resident_id, resident.baseline_vitals or {}):
        gaps.append(g)
    if g := await _escalating_vitals(resident_id, since):
        gaps.append(g)
    gaps.extend(await _plan_risk_unaddressed(resident_id, since, plan))
    gaps.extend(await _overdue_followups(resident_id))

    order = {"high": 0, "watch": 1, "info": 2}
    gaps.sort(key=lambda g: order.get(g.severity, 9))

    return CareGapReport(resident_id=resident_id, days_considered=days, gaps=gaps)


__all__ = ["find_care_gaps"]
