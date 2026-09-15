"""Presentation-only helpers for the Step 7 dashboard templates."""

from __future__ import annotations

from app.db.models import ProcessedEmail, ProcessingStatus


def display_status(row: ProcessedEmail) -> str:
    """Map internal processing state to the vocabulary from the spec
    (added / needs review / skipped), plus the two transient/error states
    a real system also needs to show.
    """
    if row.status == ProcessingStatus.COMPLETED:
        return "added" if row.calendar_event_id else "needs review"
    if row.status == ProcessingStatus.SKIPPED:
        return "skipped"
    if row.status == ProcessingStatus.PROCESSING:
        return "processing"
    return "failed"


STATUS_BADGE_CLASS = {
    "added": "badge-added",
    "needs review": "badge-review",
    "skipped": "badge-skipped",
    "processing": "badge-processing",
    "failed": "badge-failed",
}

ACTION_TYPE_LABEL = {
    "needs_reply": "Needs reply",
    "unclear": "Unclear",
}

ACTION_TYPE_BADGE_CLASS = {
    "needs_reply": "badge-review",
    "unclear": "badge-skipped",
}
