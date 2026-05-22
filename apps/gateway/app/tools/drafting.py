"""Drafting tools for structured SIS entries."""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from uuid import UUID

from pydantic import ValidationError

from app.models import CareEvent, Resident
from app.schemas.enums import AuditAction, EventStatus, Theme
from app.schemas.sis import SIS_SCHEMAS
from app.tools._audit import audited
from app.tools._errors import NotFoundError, SchemaError
from app.tools._types import (
    DraftResult,
    FieldValidation,
    NarrativeSummary,
    RedactionResult,
    ThemeParagraph,
    ValidationResult,
)

log = logging.getLogger(__name__)


@audited(AuditAction.DRAFT_SIS_ENTRY)
async def draft_sis_entry(
    theme: Theme,
    resident_id: UUID,
    content: dict[str, Any],
    source_transcript: str,
    *,
    request_id: str,
    actor: str = "agent",
) -> DraftResult:
    """Parse content against the theme schema and persist as a draft event."""
    schema_cls = SIS_SCHEMAS.get(theme.value if isinstance(theme, Theme) else str(theme))
    if schema_cls is None:
        raise SchemaError(f"unknown theme {theme!r}; expected one of {list(SIS_SCHEMAS)}")

    resident = await Resident.get_or_none(id=resident_id)
    if resident is None:
        raise NotFoundError(f"resident {resident_id} not found")

    try:
        parsed = schema_cls.model_validate(content)
    except ValidationError as exc:
        raise SchemaError(f"content failed {theme} schema: {exc.errors()}") from exc

    payload = parsed.model_dump(mode="json", exclude_none=True)
    entry = await CareEvent.create(
        resident=resident,
        theme=Theme(theme) if not isinstance(theme, Theme) else theme,
        content=payload,
        source_transcript=source_transcript,
        status=EventStatus.DRAFT,
        created_by=actor,
        request_id=request_id,
    )
    return DraftResult(entry_id=entry.id, theme=entry.theme, parsed_content=payload)




_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _transcript_numbers(text: str) -> list[float]:
    out: list[float] = []
    for token in _NUMBER_RE.findall(text):
        try:
            out.append(float(token.replace(",", ".")))
        except ValueError:
            continue
    return out


def _ground_numeric(value: float, transcript_numbers: list[float]) -> float:
    """Score whether a value appears in transcript numbers."""
    if not transcript_numbers:
        return 0.0
    best = 0.0
    for n in transcript_numbers:
        if n == value:
            return 1.0
        if value == 0:
            continue
        delta = abs(n - value) / abs(value)
        if delta <= 0.05:
            best = max(best, 0.7)
        elif delta <= 0.10:
            best = max(best, max(best, 0.4))
    return best


def _ground_string(value: str, transcript: str) -> float:
    haystack = transcript.lower()
    if not haystack:
        return 0.0
    needle = value.strip().lower()
    if not needle:
        return 1.0
    if needle in haystack:
        return 1.0
    tokens = [t for t in re.split(r"\W+", needle) if len(t) > 2]
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in haystack)
    return min(1.0, hits / max(1, len(tokens)) * 0.8)


def _flatten(content: dict[str, Any], prefix: str = "") -> Iterable[tuple[str, Any]]:
    for key, value in content.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _flatten(value, path)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    yield from _flatten(item, f"{path}[{i}]")
                else:
                    yield f"{path}[{i}]", item
        else:
            yield path, value


def _score_field(value: Any, transcript: str, numbers: list[float]) -> tuple[float, bool]:
    if value is None:
        return 1.0, True
    if isinstance(value, bool):
        return (1.0, True) if value is False else (_ground_string("true", transcript), True)
    if isinstance(value, (int, float)):
        c = _ground_numeric(float(value), numbers)
        return c, c >= 0.6
    if isinstance(value, str):
        c = _ground_string(value, transcript)
        return c, c >= 0.6
    return 0.5, False


@audited(AuditAction.VALIDATE_ENTRY)
async def validate_entry(
    entry_id: UUID,
    source_transcript: str,
    *,
    request_id: str,
    actor: str = "agent",
) -> ValidationResult:
    """Check whether entry fields are grounded in the transcript."""
    entry = await CareEvent.get_or_none(id=entry_id)
    if entry is None:
        raise NotFoundError(f"entry {entry_id} not found")

    transcript = source_transcript or entry.source_transcript or ""
    numbers = _transcript_numbers(transcript)
    fields: list[FieldValidation] = []
    confidences: list[float] = []

    for path, value in _flatten(entry.content or {}):
        if value is None:
            continue
        confidence, grounded = _score_field(value, transcript, numbers)
        confidences.append(confidence)
        fields.append(
            FieldValidation(
                field=path,
                confidence=confidence,
                grounded=grounded,
                note=None if grounded else "field not clearly supported by transcript",
            )
        )

    overall = sum(confidences) / len(confidences) if confidences else 1.0
    passed = overall >= 0.6 and all(f.grounded for f in fields)

    entry.validator_confidence = overall
    if not passed:
        entry.status = EventStatus.NEEDS_REVIEW
    await entry.save(update_fields=["validator_confidence", "status"])

    return ValidationResult(
        entry_id=entry_id,
        overall_confidence=overall,
        fields=fields,
        passed=passed,
    )



_HEADLINE_PRIORITY = (Theme.INCIDENT, Theme.VITALS, Theme.MOBILITY, Theme.NUTRITION, Theme.COGNITION, Theme.SOCIAL)


def _para_vitals(content: dict) -> str:
    parts: list[str] = []
    if content.get("bp_systolic") and content.get("bp_diastolic"):
        parts.append(f"BP {content['bp_systolic']}/{content['bp_diastolic']} mmHg")
    if content.get("heart_rate"):
        parts.append(f"HR {content['heart_rate']} bpm")
    if content.get("temperature_c"):
        parts.append(f"Temp {content['temperature_c']}°C")
    if content.get("o2_sat"):
        parts.append(f"O2 {content['o2_sat']}%")
    line = "Vitals: " + (", ".join(parts) if parts else "no measurements recorded")
    if content.get("notes"):
        line += f". {content['notes']}"
    return line + "."


def _para_nutrition(content: dict) -> str:
    bits: list[str] = []
    appetite = content.get("appetite")
    if appetite:
        bits.append(f"appetite {appetite}")
    meals = content.get("meals") or []
    refused = [m["meal"] for m in meals if m.get("refused")]
    if refused:
        bits.append(f"refused {', '.join(refused)}")
    eaten = [f"{m['meal']} {m.get('intake_pct','?')}%" for m in meals if not m.get("refused")]
    if eaten:
        bits.append("ate " + ", ".join(eaten))
    if content.get("hydration_ml"):
        bits.append(f"{content['hydration_ml']} ml fluids")
    line = "Nutrition: " + ("; ".join(bits) if bits else "no nutrition notes")
    if content.get("notes"):
        line += f". {content['notes']}"
    return line + "."


def _para_mobility(content: dict) -> str:
    bits: list[str] = []
    if content.get("independence_level"):
        bits.append(content["independence_level"])
    if content.get("aids_used"):
        bits.append("aids: " + ", ".join(content["aids_used"]))
    if content.get("falls"):
        bits.append(f"{content['falls']} fall(s)")
    if content.get("distance_walked_m"):
        bits.append(f"walked {content['distance_walked_m']} m")
    line = "Mobility: " + ("; ".join(bits) if bits else "no mobility notes")
    if content.get("notes"):
        line += f". {content['notes']}"
    return line + "."


def _para_cognition(content: dict) -> str:
    bits = [f"{k}: {v}" for k in ("orientation", "mood", "communication") if (v := content.get(k))]
    line = "Cognition: " + ("; ".join(bits) if bits else "no cognition notes")
    if content.get("notes"):
        line += f". {content['notes']}"
    return line + "."


def _para_social(content: dict) -> str:
    bits: list[str] = []
    if content.get("interactions"):
        bits.append("interactions: " + ", ".join(content["interactions"]))
    if content.get("family_contact"):
        bits.append(f"family contact: {content['family_contact']}")
    if content.get("mood_observed"):
        bits.append(f"mood observed: {content['mood_observed']}")
    line = "Social: " + ("; ".join(bits) if bits else "no social notes")
    if content.get("notes"):
        line += f". {content['notes']}"
    return line + "."


def _para_incident(content: dict) -> str:
    line = f"Incident ({content.get('severity', 'unknown')}): {content.get('type', 'unspecified')}"
    if content.get("description"):
        line += f" — {content['description']}"
    if content.get("action_taken"):
        line += f". Action: {content['action_taken']}"
    return line + "."


_THEME_RENDERERS = {
    Theme.VITALS: _para_vitals,
    Theme.NUTRITION: _para_nutrition,
    Theme.MOBILITY: _para_mobility,
    Theme.COGNITION: _para_cognition,
    Theme.SOCIAL: _para_social,
    Theme.INCIDENT: _para_incident,
}


def _headline(by_theme: dict[Theme, dict]) -> str:
    for theme in _HEADLINE_PRIORITY:
        if theme in by_theme:
            if theme == Theme.INCIDENT:
                return f"Incident recorded ({by_theme[theme].get('severity','?')})."
            if theme == Theme.VITALS:
                sys = by_theme[theme].get("bp_systolic")
                dia = by_theme[theme].get("bp_diastolic")
                if sys and dia:
                    return f"Vitals recorded (BP {sys}/{dia})."
                return "Vitals recorded."
            return f"{theme.value.capitalize()} notes recorded."
    return "Shift documentation complete."


@audited(AuditAction.SYNTHESIZE_SUMMARY)
async def synthesize_summary(
    entry_ids: list[UUID],
    *,
    request_id: str,
    actor: str = "agent",
) -> NarrativeSummary:
    """Render confirmed drafts as a SIS narrative."""
    if not entry_ids:
        return NarrativeSummary(entry_ids=[], headline="No entries to summarise.", paragraphs=[])

    entries = await CareEvent.filter(id__in=entry_ids).order_by("created_at").all()
    by_theme: dict[Theme, dict] = {}
    for e in entries:
        by_theme[e.theme] = e.content or {}

    paragraphs: list[ThemeParagraph] = []
    for theme in _HEADLINE_PRIORITY:
        if theme in by_theme:
            renderer = _THEME_RENDERERS[theme]
            paragraphs.append(ThemeParagraph(theme=theme, text=renderer(by_theme[theme])))

    return NarrativeSummary(
        entry_ids=[e.id for e in entries],
        headline=_headline(by_theme),
        paragraphs=paragraphs,
    )



@audited(AuditAction.REDACT_PII)
async def redact_pii(
    text: str,
    *,
    request_id: str,
    actor: str = "agent",
    extra_names: list[str] | None = None,
) -> RedactionResult:
    if not text:
        return RedactionResult(redacted_text="", mapping={})

    names: set[str] = set(extra_names or [])
    async for r in Resident.all():
        if r.first_name:
            names.add(r.first_name)
        if r.last_name:
            names.add(r.last_name)
    ordered = sorted((n for n in names if n), key=len, reverse=True)

    mapping: dict[str, str] = {}
    redacted = text
    counter = 1
    for name in ordered:
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        if not pattern.search(redacted):
            continue
        token = f"[RESIDENT_{counter}]"
        counter += 1
        mapping[token] = name
        redacted = pattern.sub(token, redacted)

    return RedactionResult(redacted_text=redacted, mapping=mapping)


__all__ = [
    "draft_sis_entry",
    "validate_entry",
    "synthesize_summary",
    "redact_pii",
]
