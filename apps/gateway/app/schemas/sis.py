"""SIS-aligned Pydantic schemas.

These are the structured-output targets for the agent's extraction step
and the reference shape that the validator agent enforces against the
source transcript.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.enums import AppetiteLevel, IncidentSeverity, IndependenceLevel


class Vitals(BaseModel):
    bp_systolic: int | None = Field(None, ge=40, le=260)
    bp_diastolic: int | None = Field(None, ge=30, le=160)
    heart_rate: int | None = Field(None, ge=20, le=220)
    temperature_c: float | None = Field(None, ge=30.0, le=43.0)
    o2_sat: int | None = Field(None, ge=50, le=100)
    weight_kg: float | None = Field(None, ge=20, le=300)
    measured_at: datetime | None = None
    notes: str | None = None


class Meal(BaseModel):
    meal: str
    intake_pct: int | None = Field(None, ge=0, le=100)
    refused: bool = False
    reason: str | None = None


class Nutrition(BaseModel):
    meals: list[Meal] = Field(default_factory=list)
    hydration_ml: int | None = Field(None, ge=0, le=5000)
    appetite: AppetiteLevel | None = None
    notes: str | None = None


class Mobility(BaseModel):
    independence_level: IndependenceLevel | None = None
    aids_used: list[str] = Field(default_factory=list)
    falls: int = Field(0, ge=0)
    distance_walked_m: int | None = Field(None, ge=0)
    notes: str | None = None


class Cognition(BaseModel):
    orientation: str | None = None
    mood: str | None = None
    communication: str | None = None
    notes: str | None = None


class Social(BaseModel):
    interactions: list[str] = Field(default_factory=list)
    family_contact: str | None = None
    mood_observed: str | None = None
    notes: str | None = None


class Incident(BaseModel):
    type: str
    severity: IncidentSeverity
    description: str
    action_taken: str | None = None
    reported_to: str | None = None
    occurred_at: datetime | None = None


SIS_SCHEMAS: dict[str, type[BaseModel]] = {
    "vitals": Vitals,
    "nutrition": Nutrition,
    "mobility": Mobility,
    "cognition": Cognition,
    "social": Social,
    "incident": Incident,
}
