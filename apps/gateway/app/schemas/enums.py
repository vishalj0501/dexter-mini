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
    TOOL_CALL = "tool_call"
    LLM_CALL = "llm_call"
    DB_WRITE = "db_write"
    FINALIZE = "finalize"
    FLAG = "flag"
    ASK_HUMAN = "ask_human"
    LOGIN = "login"
