"""The review actions a person can take on an extraction, shared by the dashboard (app/main.py)
and the Gmail add-on's API (app/addon_api.py) so the two can never disagree about what an action
does or when it is allowed.

Each function validates, talks to Google Calendar where the action needs to, and updates the row.
A refused action raises ActionError with the HTTP status the caller should answer with; this module
knows nothing about web frameworks.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app import calendar_client, gmail_client
from app.db import repository
from app.db.models import ActionType, ProcessedEmail, ProcessingStatus


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


def approve_at(db: Session, email_id: str, deadline: datetime) -> ProcessedEmail:
    """Add a held-back deadline to the calendar at a time the person chose ("Reschedule" on a needs-review item).

    approve() adds it at the date the pipeline extracted, and refuses when there is none. This is how to add
    one whose extracted date is wrong, or that has no date at all. It creates a one-off event at the chosen
    time (a recurrence rule extracted for a different date would not be trustworthy). Once the person has
    picked the time, any "implausible date" warning about the extracted one no longer applies.
    """
    row = _get(db, email_id)
    is_deadline = row.extraction_action_type in (None, ActionType.DEADLINE)
    if row.status != ProcessingStatus.COMPLETED or row.calendar_event_id or not is_deadline:
        raise ActionError(400, "Not a held-back item")

    service = calendar_client.get_calendar_service()
    event_id = calendar_client.create_event(
        service,
        summary=row.extraction_event_name or row.email_subject,
        description=row.extraction_source_context or "",
        deadline=deadline,
        has_time=True,
    )
    row = repository.schedule_action_item(db, email_id, deadline, event_id)  # records the time and the event
    row.is_implausible_date = False
    db.commit()
    return row


def reschedule_event(db: Session, email_id: str, deadline: datetime) -> ProcessedEmail:
    """The "wrong, but here's the right time" path for a row with a live Calendar event — patches
    it in place rather than deleting and recreating. Leaves any recorded vote untouched: rescheduling
    and voting are independent (docs/design-decisions.md, decision 15)."""
    row = _get(db, email_id)
    if not row.calendar_event_id:
        raise ActionError(400, "No live Calendar event to reschedule")

    service = calendar_client.get_calendar_service()
    calendar_client.update_event(
        service,
        event_id=row.calendar_event_id,
        summary=row.extraction_event_name or row.email_subject,
        description=row.extraction_source_context or "",
        deadline=deadline,
        has_time=True,
    )
    return repository.reschedule_email(db, email_id, deadline, has_time=True)


def schedule_action_item(db: Session, email_id: str, deadline: datetime) -> ProcessedEmail:
    """An action item has no date, so no Calendar event yet. This creates one at a time the person
    picked and moves the item out of Action Items into the regular list, where it behaves like any
    other scheduled deadline (Reschedule / Remove)."""
    row = _get(db, email_id)
    is_action_item = row.extraction_action_type in (ActionType.NEEDS_REPLY, ActionType.UNCLEAR)
    if not is_action_item or row.status != ProcessingStatus.COMPLETED or row.calendar_event_id:
        raise ActionError(400, "Not an unscheduled action item")

    service = calendar_client.get_calendar_service()
    event_id = calendar_client.create_event(
        service,
        summary=row.extraction_event_name or row.email_subject,
        description=row.extraction_source_context or "",
        deadline=deadline,
        has_time=True,
    )
    return repository.schedule_action_item(db, email_id, deadline, event_id)


def decline(db: Session, email_id: str) -> ProcessedEmail:
    """The "don't add" side of the same checkmark: permanently skip, creating no Calendar event."""
    try:
        repository.mark_skipped(db, email_id, "declined by user (low-confidence review)")
    except ValueError:
        raise ActionError(404, "No such email") from None
    return _get(db, email_id)


def trash_email(db: Session, email_id: str) -> ProcessedEmail:
    """Move the source Gmail message to Trash (recoverable there for 30 days — the same thing
    Gmail's own trash icon does, never a permanent delete) and hide the row everywhere on the
    dashboard. Deliberately orthogonal to Remove: this is about the email, not about an existing
    Calendar event or the extraction's accuracy — either of those stays untouched."""
    row = _get(db, email_id)
    service = gmail_client.get_gmail_service()
    gmail_client.trash_message(service, row.email_id)
    return repository.trash_email(db, email_id)
