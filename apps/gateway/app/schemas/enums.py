from enum import Enum


class Theme(str, Enum):
    VITALS = "vitals"
    NUTRITION = "nutrition"
    MOBILITY = "mobility"
    COGNITION = "cognition"
    SOCIAL = "social"
    INCIDENT = "incident"


class EventStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    FINAL = "final"


class IndependenceLevel(str, Enum):
    INDEPENDENT = "independent"
    SUPERVISED = "supervised"
    ASSISTED = "assisted"
    DEPENDENT = "dependent"


class AppetiteLevel(str, Enum):
    GOOD = "good"
    REDUCED = "reduced"
    POOR = "poor"
    REFUSED = "refused"


class IncidentSeverity(str, Enum):
    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"


class FlagSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuditAction(str, Enum):

    LLM_CALL = "llm_call"
    LOGIN = "login"


    GET_RESIDENT = "tool.get_resident"
    GET_RECENT_NOTES = "tool.get_recent_notes"
    SEARCH_CARE_PLAN = "tool.search_care_plan"
    CHECK_VITAL_RANGES = "tool.check_vital_ranges"

    DRAFT_SIS_ENTRY = "tool.draft_sis_entry"
    VALIDATE_ENTRY = "tool.validate_entry"
    SYNTHESIZE_SUMMARY = "tool.synthesize_summary"
    REDACT_PII = "tool.redact_pii"

    ASK_CAREGIVER = "tool.ask_caregiver"
    FLAG_FOR_REVIEW = "tool.flag_for_review"
    SCHEDULE_FOLLOWUP = "tool.schedule_followup"
    FINALIZE_ENTRY = "tool.finalize_entry"
    LIST_PENDING_DOCUMENTATION = "tool.list_pending_documentation"
    FIND_CARE_GAPS = "tool.find_care_gaps"


class FollowupStatus(str, Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"
