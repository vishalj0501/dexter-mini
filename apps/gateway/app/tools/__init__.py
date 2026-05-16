"""Plain-Python tool catalog for the shift-copilot agent.

Each tool is an `async def` decorated with `@audited(AuditAction.…)` and lives
in one of three modules grouped by domain. The grouping is for readability —
they're all in-process function calls; there is no HTTP, no MCP, no serializer
between the agent and a tool.
"""

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
    # resident
    "get_resident",
    "get_recent_notes",
    "search_care_plan",
    "check_vital_ranges",
    # drafting
    "draft_sis_entry",
    "validate_entry",
    "synthesize_summary",
    "redact_pii",
    # workflow
    "ask_caregiver",
    "flag_for_review",
    "schedule_followup",
    "finalize_entry",
    "list_pending_documentation",
]
