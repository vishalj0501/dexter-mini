from datetime import datetime, timezone

from tortoise import fields, models

from app.schemas.enums import AuditAction, EventStatus, FlagSeverity, FollowupStatus, Theme


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Resident(models.Model):
    id = fields.UUIDField(pk=True)
    first_name = fields.CharField(max_length=128)
    last_name = fields.CharField(max_length=128)
    room_number = fields.CharField(max_length=32)
    date_of_birth = fields.DateField()
    admitted_at = fields.DateField()
    baseline_vitals = fields.JSONField(default=dict)
    notes = fields.TextField(default="")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    care_plan: fields.ReverseRelation["CarePlan"]
    care_events: fields.ReverseRelation["CareEvent"]
    review_flags: fields.ReverseRelation["ReviewFlag"]
    followups: fields.ReverseRelation["Followup"]

    class Meta:
        table = "residents"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def __str__(self) -> str:
        return f"{self.full_name} (Room {self.room_number})"


class CarePlan(models.Model):
    id = fields.UUIDField(pk=True)
    resident: fields.ForeignKeyRelation = fields.ForeignKeyField(
        "models.Resident", related_name="care_plan", on_delete=fields.CASCADE
    )
    goals = fields.JSONField(default=list)
    risk_flags = fields.JSONField(default=list)
    dietary_restrictions = fields.TextField(default="")
    mobility_status = fields.TextField(default="")
    active = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "care_plans"


class CareEvent(models.Model):
    id = fields.UUIDField(pk=True)
    resident: fields.ForeignKeyRelation = fields.ForeignKeyField(
        "models.Resident", related_name="care_events", on_delete=fields.CASCADE
    )
    theme = fields.CharEnumField(Theme, max_length=32)
    content = fields.JSONField(default=dict)
    source_transcript = fields.TextField(default="")
    status = fields.CharEnumField(EventStatus, default=EventStatus.DRAFT, max_length=32)
    created_by = fields.CharField(max_length=128, default="agent")
    request_id = fields.CharField(max_length=64, null=True)
    validator_confidence = fields.FloatField(null=True)

    created_at = fields.DatetimeField(default=_utcnow)
    finalized_at = fields.DatetimeField(null=True)

    class Meta:
        table = "care_events"
        indexes = [("resident_id", "created_at"), ("status",)]


class ReviewFlag(models.Model):
    id = fields.UUIDField(pk=True)
    resident: fields.ForeignKeyRelation = fields.ForeignKeyField(
        "models.Resident", related_name="review_flags", on_delete=fields.CASCADE
    )
    reason = fields.TextField()
    severity = fields.CharEnumField(FlagSeverity, default=FlagSeverity.MEDIUM, max_length=16)
    raised_by = fields.CharField(max_length=128, default="agent")
    request_id = fields.CharField(max_length=64, null=True)
    resolved = fields.BooleanField(default=False)
    resolved_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "review_flags"
        indexes = [("resident_id", "resolved")]


class Followup(models.Model):
    id = fields.UUIDField(pk=True)
    resident: fields.ForeignKeyRelation = fields.ForeignKeyField(
        "models.Resident", related_name="followups", on_delete=fields.CASCADE
    )
    action = fields.TextField()
    due_at = fields.DatetimeField()
    status = fields.CharEnumField(FollowupStatus, default=FollowupStatus.OPEN, max_length=16)
    raised_by = fields.CharField(max_length=128, default="agent")
    request_id = fields.CharField(max_length=64, null=True)
    completed_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "followups"
        indexes = [("resident_id", "status"), ("due_at",)]


class AuditLog(models.Model):
    id = fields.UUIDField(pk=True)
    request_id = fields.CharField(max_length=64, index=True)
    action = fields.CharEnumField(AuditAction, max_length=32)
    actor = fields.CharField(max_length=128)
    payload = fields.JSONField(default=dict)
    latency_ms = fields.IntField(null=True)
    cost_usd = fields.FloatField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "audit_log"
        indexes = [("request_id", "created_at")]


class EvalRun(models.Model):
    id = fields.UUIDField(pk=True)
    scenario_id = fields.CharField(max_length=128)
    git_sha = fields.CharField(max_length=40, null=True)
    config = fields.JSONField(default=dict)

    field_accuracy = fields.FloatField(null=True)
    hallucination_rate = fields.FloatField(null=True)
    tool_selection_accuracy = fields.FloatField(null=True)
    asked_when_should_have = fields.FloatField(null=True)
    flagged_when_should_have = fields.FloatField(null=True)
    schema_validity_rate = fields.FloatField(null=True)
    reliability_rate = fields.FloatField(null=True)

    cost_usd = fields.FloatField(null=True)
    latency_p50_ms = fields.IntField(null=True)
    latency_p95_ms = fields.IntField(null=True)

    raw_results = fields.JSONField(default=dict)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "eval_runs"
        indexes = [("scenario_id", "created_at")]


__all__ = [
    "AuditLog",
    "CareEvent",
    "CarePlan",
    "EvalRun",
    "Followup",
    "Resident",
    "ReviewFlag",
]
