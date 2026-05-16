"""Test setup: fresh in-memory SQLite per test, plus a minimal resident factory.

We pick SQLite (not Postgres) for unit tests so a developer can run the suite
in <2 seconds without docker. The tools themselves don't use any
Postgres-specific features at this layer — Tortoise abstracts JSON and enums.
Integration tests against real Postgres will land later when CI is wired up.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest_asyncio
from tortoise import Tortoise

from app.models import CarePlan, Resident
from app.schemas.enums import FlagSeverity  # noqa: F401  (re-exported for tests)


TEST_CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {
        "models": {
            "models": ["app.models"],
            "default_connection": "default",
        }
    },
    "use_tz": True,
    "timezone": "UTC",
}


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    await Tortoise.init(config=TEST_CONFIG, _create_db=False)
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise._drop_databases()
        await Tortoise.close_connections()


@pytest_asyncio.fixture
async def resident() -> Resident:
    """One realistic resident with an active care plan."""
    r = await Resident.create(
        id=uuid.uuid4(),
        first_name="Margarethe",
        last_name="Müller",
        room_number="12",
        date_of_birth=date(1938, 4, 12),
        admitted_at=date(2023, 1, 15),
        baseline_vitals={"bp_systolic": 130, "bp_diastolic": 80, "heart_rate": 72},
        notes="Refers to herself as Frau Müller.",
    )
    await CarePlan.create(
        resident=r,
        goals=["Maintain mobility", "Monitor nutrition"],
        risk_flags=["fall_risk", "hypertension"],
        dietary_restrictions="Low salt.",
        mobility_status="Uses walker.",
    )
    return r


@pytest_asyncio.fixture
async def other_resident() -> Resident:
    """A second resident — used for ambiguity and pending-doc tests."""
    r = await Resident.create(
        id=uuid.uuid4(),
        first_name="Hans",
        last_name="Müller",  # Same surname → ambiguity scenario
        room_number="14",
        date_of_birth=date(1940, 9, 3),
        admitted_at=date(2024, 3, 1),
        baseline_vitals={"bp_systolic": 128, "bp_diastolic": 78, "heart_rate": 68},
        notes="",
    )
    await CarePlan.create(resident=r, goals=[], risk_flags=[], dietary_restrictions="", mobility_status="")
    return r


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


REQUEST_ID = "test-req-00000000"
