"""The review actions a person can take on an extraction, shared by the dashboard (app/main.py)
and the Gmail add-on's API (app/addon_api.py) so the two can never disagree about what an action
does or when it is allowed.

Each function validates, talks to Google Calendar where the action needs to, and updates the row.
A refused action raises ActionError with the HTTP status the caller should answer with; this module
knows nothing about web frameworks.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import calendar_client
from app.db import repository
from app.db.models import ProcessedEmail, ProcessingStatus


class ActionError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _get(db: Session, email_id: str) -> ProcessedEmail:
    row = db.get(ProcessedEmail, email_id)
    if row is None:
        raise ActionError(404, "No such email")
    return row


def record_vote(db: Session, email_id: str, is_correct: bool) -> ProcessedEmail:
    """A plain correct/incorrect verdict. It records the vote and nothing else: it never touches
    the Calendar, even on a row with a live event."""
    _get(db, email_id)
    return repository.set_correction(db, email_id, is_correct)


def remove_event(db: Session, email_id: str) -> ProcessedEmail:
    """The "wrong, just get rid of it" path: deletes the real Calendar event, not just the local
    record of it. The row is kept (so it is never re-added) but is no longer listed."""
    row = _get(db, email_id)
    if not row.calendar_event_id:
        raise ActionError(400, "No live Calendar event to remove")

    service = calendar_client.get_calendar_service()
    calendar_client.delete_event(service, row.calendar_event_id)
    return repository.remove_calendar_event(db, email_id)


def approve(db: Session, email_id: str) -> ProcessedEmail:
    """A low-confidence "needs review" checkmark: create the Calendar event the pipeline held back
    on (docs/design-decisions.md, decision 9)."""
    row = _get(db, email_id)
    if row.status != ProcessingStatus.COMPLETED or row.calendar_event_id or not row.extraction_deadline_parsed:
        raise ActionError(400, "Not an approvable item")

    service = calendar_client.get_calendar_service()
    event_id = calendar_client.create_event(
        service,
        summary=row.extraction_event_name or row.email_subject,
        description=row.extraction_source_context or "",
        deadline=row.extraction_deadline_parsed,
        has_time=bool(row.extraction_has_time),
        recurrence_rule=row.extraction_recurrence_rule if row.extraction_is_recurring else None,
    )
    return repository.set_calendar_event(db, email_id, event_id)


def decline(db: Session, email_id: str) -> ProcessedEmail:
    """The "don't add" side of the same checkmark: permanently skip, creating no Calendar event."""
    try:
        repository.mark_skipped(db, email_id, "declined by user (low-confidence review)")
    except ValueError:
        raise ActionError(404, "No such email") from None
    return _get(db, email_id)
