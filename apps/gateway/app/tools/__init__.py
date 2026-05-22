"""Plain-Python tool catalog for the shift-copilot agent."""

from app.tools.drafting import (
    draft_sis_entry,
    redact_pii,
    synthesize_summary,
    validate_entry,
)
from app.tools.resident import (
    check_vital_ranges,
    get_recent_notes,
    get_resident,
    search_care_plan,
)
from app.tools.workflow import (
    ask_caregiver,
    finalize_entry,
    flag_for_review,
    list_pending_documentation,
    schedule_followup,
)

__all__ = [
    "get_resident",
    "get_recent_notes",
    "search_care_plan",
    "check_vital_ranges",
    "draft_sis_entry",
    "validate_entry",
    "synthesize_summary",
    "redact_pii",
    "ask_caregiver",
    "flag_for_review",
    "schedule_followup",
    "finalize_entry",
    "list_pending_documentation",
]
