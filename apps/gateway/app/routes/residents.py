"""Read-only resident listings for the frontend."""

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
