"""Seed demo residents with care plans and historical events."""

import logging
import random
from datetime import date, datetime, timedelta, timezone

from app.models import CareEvent, CarePlan, Resident
from app.schemas.enums import EventStatus, Theme

log = logging.getLogger(__name__)


RESIDENTS: list[dict] = [
    {
        "first_name": "Margarethe",
        "last_name": "Müller",
        "room_number": "12",
        "date_of_birth": date(1938, 4, 12),
        "admitted_at": date(2023, 1, 15),
        "baseline_vitals": {"bp_systolic": 135, "bp_diastolic": 85, "heart_rate": 72},
        "notes": "Refers to herself as Frau Müller. Daughter visits on Sundays.",
        "care_plan": {
            "goals": ["Maintain mobility", "Monitor nutrition", "Encourage social engagement"],
            "risk_flags": ["fall_risk", "hypertension"],
            "dietary_restrictions": "Low salt.",
            "mobility_status": "Uses walker; ambulates with supervision.",
        },
    },
    {
        "first_name": "Hans",
        "last_name": "Schmidt",
        "room_number": "14",
        "date_of_birth": date(1940, 9, 3),
        "admitted_at": date(2024, 3, 1),
        "baseline_vitals": {"bp_systolic": 128, "bp_diastolic": 78, "heart_rate": 68},
        "notes": "Retired carpenter. Prefers male caregivers for personal care.",
        "care_plan": {
            "goals": ["Manage diabetes", "Maintain independence"],
            "risk_flags": ["diabetes_type2"],
            "dietary_restrictions": "Diabetic diet, no added sugar.",
            "mobility_status": "Independent ambulation.",
        },
    },
    {
        "first_name": "Ingrid",
        "last_name": "Weber",
        "room_number": "18",
        "date_of_birth": date(1932, 12, 28),
        "admitted_at": date(2022, 6, 10),
        "baseline_vitals": {"bp_systolic": 142, "bp_diastolic": 88, "heart_rate": 80},
        "notes": "Mid-stage dementia. Responds well to music.",
        "care_plan": {
            "goals": ["Maintain orientation cues", "Prevent agitation"],
            "risk_flags": ["dementia", "wandering_risk", "fall_risk"],
            "dietary_restrictions": "None.",
            "mobility_status": "Walks with supervision; supervised mealtimes.",
        },
    },
    {
        "first_name": "Walter",
        "last_name": "Becker",
        "room_number": "21",
        "date_of_birth": date(1945, 7, 15),
        "admitted_at": date(2024, 8, 22),
        "baseline_vitals": {"bp_systolic": 118, "bp_diastolic": 72, "heart_rate": 64},
        "notes": "Recovering from hip replacement. PT three times weekly.",
        "care_plan": {
            "goals": ["Post-op mobility recovery", "Pain management"],
            "risk_flags": ["post_surgical", "fall_risk"],
            "dietary_restrictions": "High protein.",
            "mobility_status": "Wheelchair plus walker; progressing with PT.",
        },
    },
    {
        "first_name": "Helga",
        "last_name": "Schneider",
        "room_number": "23",
        "date_of_birth": date(1936, 2, 8),
        "admitted_at": date(2023, 11, 5),
        "baseline_vitals": {"bp_systolic": 132, "bp_diastolic": 82, "heart_rate": 75},
        "notes": "Devout; appreciates morning prayer time before care.",
        "care_plan": {
            "goals": ["Maintain mobility", "Skin integrity monitoring"],
            "risk_flags": ["pressure_ulcer_risk"],
            "dietary_restrictions": "Pureed diet (dysphagia).",
            "mobility_status": "Independent transfers; uses cane.",
        },
    },
    {
        "first_name": "Otto",
        "last_name": "Fischer",
        "room_number": "27",
        "date_of_birth": date(1941, 11, 19),
        "admitted_at": date(2024, 1, 12),
        "baseline_vitals": {"bp_systolic": 145, "bp_diastolic": 92, "heart_rate": 78},
        "notes": "Former engineer. Reads the daily newspaper.",
        "care_plan": {
            "goals": ["Cardiac monitoring", "Maintain cognitive engagement"],
            "risk_flags": ["chf", "hypertension"],
            "dietary_restrictions": "Low sodium, fluid restriction 1500 ml/day.",
            "mobility_status": "Independent.",
        },
    },
    {
        "first_name": "Erika",
        "last_name": "Hoffmann",
        "room_number": "29",
        "date_of_birth": date(1934, 5, 30),
        "admitted_at": date(2022, 10, 8),
        "baseline_vitals": {"bp_systolic": 138, "bp_diastolic": 84, "heart_rate": 82},
        "notes": "Hard of hearing; speak slowly and clearly.",
        "care_plan": {
            "goals": ["Daily hearing-aid use", "Social engagement"],
            "risk_flags": ["hearing_impaired", "fall_risk"],
            "dietary_restrictions": "None.",
            "mobility_status": "Uses walker.",
        },
    },
    {
        "first_name": "Friedrich",
        "last_name": "Wagner",
        "room_number": "31",
        "date_of_birth": date(1939, 8, 21),
        "admitted_at": date(2023, 5, 17),
        "baseline_vitals": {"bp_systolic": 125, "bp_diastolic": 78, "heart_rate": 70},
        "notes": "Family lives abroad; weekly Sunday video calls.",
        "care_plan": {
            "goals": ["Maintain mood", "Bowel regularity monitoring"],
            "risk_flags": ["depression_history", "constipation"],
            "dietary_restrictions": "High fiber.",
            "mobility_status": "Independent.",
        },
    },
]


async def seed_if_empty() -> None:
    if await Resident.exists():
        log.info("seed: residents already present, skipping")
        return

    log.info("seed: inserting %d residents with history", len(RESIDENTS))
    for spec in RESIDENTS:
        plan = spec["care_plan"]
        resident_fields = {k: v for k, v in spec.items() if k != "care_plan"}
        resident = await Resident.create(**resident_fields)
        await CarePlan.create(resident=resident, **plan)
        await _seed_history(resident)
    log.info("seed: done")


async def _seed_history(resident: Resident) -> None:
    """Seed vitals and nutrition history for one resident."""
    now = datetime.now(timezone.utc)
    rng = random.Random(str(resident.id))
    baseline = resident.baseline_vitals or {}

    for days_ago in range(7, 0, -1):
        when = now - timedelta(days=days_ago, hours=rng.randint(0, 4))

        bp_s = baseline.get("bp_systolic", 130) + rng.randint(-10, 12)
        bp_d = baseline.get("bp_diastolic", 80) + rng.randint(-6, 8)
        hr = baseline.get("heart_rate", 72) + rng.randint(-5, 5)
        await CareEvent.create(
            resident=resident,
            theme=Theme.VITALS,
            content={
                "bp_systolic": bp_s,
                "bp_diastolic": bp_d,
                "heart_rate": hr,
                "measured_at": when.isoformat(),
            },
            source_transcript=f"BP {bp_s} over {bp_d}, pulse {hr}.",
            status=EventStatus.FINAL,
            created_by="seed",
            created_at=when,
            finalized_at=when,
        )

        if days_ago % 2 == 0:
            await CareEvent.create(
                resident=resident,
                theme=Theme.NUTRITION,
                content={
                    "meals": [
                        {"meal": "breakfast", "intake_pct": rng.choice([60, 75, 100]), "refused": False},
                        {"meal": "lunch", "intake_pct": rng.choice([50, 80, 100]), "refused": False},
                    ],
                    "appetite": rng.choice(["good", "reduced"]),
                },
                source_transcript="Ate breakfast and lunch.",
                status=EventStatus.FINAL,
                created_by="seed",
                created_at=when,
                finalized_at=when,
            )
