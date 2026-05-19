"""Per-run debug endpoint: every audit row, every draft, every flag, every
follow-up touched by a specific request_id. Lets the frontend show the agent's
actual DB writes, not just the parsed text actions it claimed.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.models import AuditLog, CareEvent, Followup, ReviewFlag

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/{request_id}")
async def get_run(request_id: str) -> dict:
    audit_rows = await AuditLog.filter(request_id=request_id).order_by("created_at")
    care_events = await CareEvent.filter(request_id=request_id).order_by("created_at")
    flags = await ReviewFlag.filter(request_id=request_id).order_by("created_at")
    followups = await Followup.filter(request_id=request_id).order_by("created_at")

    return {
        "request_id": request_id,
        "audit": [
            {
                "id": str(r.id),
                "action": r.action.value if hasattr(r.action, "value") else str(r.action),
                "actor": r.actor,
                "payload": r.payload,
                "latency_ms": r.latency_ms,
                "cost_usd": r.cost_usd,
                "created_at": r.created_at.isoformat(),
            }
            for r in audit_rows
        ],
        "drafts": [
            {
                "id": str(e.id),
                "theme": e.theme.value if hasattr(e.theme, "value") else str(e.theme),
                "content": e.content,
                "source_transcript": e.source_transcript,
                "status": e.status.value if hasattr(e.status, "value") else str(e.status),
                "validator_confidence": e.validator_confidence,
                "created_at": e.created_at.isoformat(),
            }
            for e in care_events
        ],
        "flags": [
            {
                "id": str(f.id),
                "reason": f.reason,
                "severity": f.severity.value if hasattr(f.severity, "value") else str(f.severity),
                "resolved": f.resolved,
                "created_at": f.created_at.isoformat(),
            }
            for f in flags
        ],
        "followups": [
            {
                "id": str(f.id),
                "action": f.action,
                "due_at": f.due_at.isoformat(),
                "status": f.status.value if hasattr(f.status, "value") else str(f.status),
                "created_at": f.created_at.isoformat(),
            }
            for f in followups
        ],
    }
