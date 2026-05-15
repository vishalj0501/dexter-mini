from fastapi import APIRouter
from tortoise import Tortoise

from app.models import Resident

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    db_ok = False
    resident_count = 0
    try:
        conn = Tortoise.get_connection("default")
        await conn.execute_query("SELECT 1")
        db_ok = True
        resident_count = await Resident.all().count()
    except Exception:
        pass
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "down",
        "residents_seeded": resident_count,
    }
