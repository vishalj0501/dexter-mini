"""Read-only resident listings for the frontend.

Not how the agent reads residents — the agent uses `get_resident` (which is
a fuzzy lookup with audit logging). This is a flat list for UI sidebars and
debugging: "which residents am I working with?"
"""

from __future__ import annotations

from fastapi import APIRouter

from app.models import Resident

router = APIRouter(prefix="/residents", tags=["residents"])


@router.get("")
async def list_residents() -> dict:
    rs = await Resident.all().order_by("room_number")
    return {
        "residents": [
            {
                "id": str(r.id),
                "full_name": r.full_name,
                "room_number": r.room_number,
                "date_of_birth": r.date_of_birth.isoformat(),
            }
            for r in rs
        ]
    }
