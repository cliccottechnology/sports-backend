"""Customization workflow: reviewer detection + valid status transitions."""
from .models import CustomizationRequest

REVIEW_PERMS = ("customization.review", "customization.manage")


def is_reviewer(user) -> bool:
    """Super Admin / anyone granted review or manage powers."""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return any(user.has_perm_code(code) for code in REVIEW_PERMS)


# Valid forward transitions for each status. A rejected/cancelled request is
# never treated as approved work; reopening follows an explicit transition.
TRANSITIONS = {
    "draft": ["submitted", "cancelled"],
    "submitted": ["under_review", "clarification_required", "approved", "rejected", "cancelled"],
    "under_review": ["clarification_required", "approved", "rejected", "cancelled"],
    "clarification_required": ["under_review", "submitted", "cancelled"],
    "approved": ["assigned", "in_progress", "completed", "cancelled"],
    "assigned": ["in_progress", "approved", "cancelled"],
    "in_progress": ["ready_for_review", "assigned", "cancelled"],
    "ready_for_review": ["completed", "in_progress", "cancelled"],
    "completed": ["in_progress", "cancelled"],
    "rejected": ["submitted", "under_review", "cancelled"],
    "cancelled": [],
}

OPEN_STATUSES = CustomizationRequest.OPEN_STATUSES


def transition_allowed(from_status: str, to_status: str) -> bool:
    if from_status == to_status:
        return True
    return to_status in TRANSITIONS.get(from_status, [])
