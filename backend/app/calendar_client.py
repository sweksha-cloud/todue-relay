"""Google Calendar event creation (Step 4).

Shares OAuth with the Gmail client — see app/google_auth.py.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import EVENT_DURATION_MINUTES
from app.date_utils import detect_local_timezone
from app.google_auth import get_google_credentials


def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_credentials())


def build_event_body(
    summary: str,
    description: str,
    deadline: datetime,
    has_time: bool,
    recurrence_rule: str | None = None,
) -> dict:
    """A timed deadline becomes a short block (EVENT_DURATION_MINUTES) at
    that time; a date-only deadline becomes a single all-day event.

    recurrence_rule (an RRULE value with no "RRULE:" prefix, e.g.
    "FREQ=MONTHLY;BYMONTHDAY=1") turns the event into a true recurring
    series instead of a one-off — see docs/design-decisions.md, decision 7.
    """
    if has_time:
        tz = detect_local_timezone()
        # Attach the offset directly rather than relying on the API to
        # interpret a naive dateTime via the separate timeZone field — a
        # real created event showed Google treating the naive value as UTC
        # regardless of timeZone (16:45 sent -> stored as 09:45-07:00,
        # i.e. it read our "16:45" as 16:45 UTC, not 16:45 local).
        aware_deadline = deadline if deadline.tzinfo else deadline.replace(tzinfo=ZoneInfo(tz))
        end = aware_deadline + timedelta(minutes=EVENT_DURATION_MINUTES)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": aware_deadline.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }
    else:
        start_date = deadline.date()
        end_date = start_date + timedelta(days=1)  # Calendar all-day events use an exclusive end date
        body = {
            "summary": summary,
            "description": description,
            "start": {"date": start_date.isoformat()},
            "end": {"date": end_date.isoformat()},
        }

    if recurrence_rule:
        body["recurrence"] = [f"RRULE:{recurrence_rule}"]

    return body


def event_id_for_email(email_id: str) -> str:
    """The Calendar event id the pipeline uses when it auto-creates the event for one email.

    Deterministic, so the create is safe to repeat. Creating the event and recording its id in
    Postgres are two separate writes; a run that dies between them leaves the claim to go stale,
    and the retry would otherwise create a second event. With the same id, the retry's insert is
    refused as already existing instead (see create_event).

    Calendar ids may contain only base32hex characters (a-v, 0-9) and must be 5 to 1024 long, so
    the email id is hashed to hex rather than used as is.
    """
    return "todue" + hashlib.sha256(email_id.encode()).hexdigest()[:32]


def create_event(
    service,
    summary: str,
    description: str,
    deadline: datetime,
    has_time: bool,
    calendar_id: str = "primary",
    recurrence_rule: str | None = None,
    event_id: str | None = None,
) -> str:
    """Create the event and return its Calendar event id (stored in
    ProcessedEmail.calendar_event_id for the audit trail / idempotency).
    For a recurring series, this id is the master event's id — patch/delete
    against it affects the whole series, not a single instance.

    With event_id, the event is created under that id, and a 409 (an event with that id already
    exists) means an earlier attempt already created it: that id is returned as a success instead
    of a second event being made. Google also keeps a deleted event's id reserved, so the same
    409 covers an event the user removed in the meantime; recording the id is still correct, and
    removing it again later is already idempotent (delete_event).
    """
    body = build_event_body(summary, description, deadline, has_time, recurrence_rule)
    if event_id is not None:
        body["id"] = event_id
    try:
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
    except HttpError as e:
        if event_id is not None and e.resp.status == 409:
            return event_id
        raise
    return created["id"]


def update_event(
    service,
    event_id: str,
    summary: str,
    description: str,
    deadline: datetime,
    has_time: bool,
    calendar_id: str = "primary",
) -> None:
    """Correct an already-created event's time in place — the "reschedule"
    side of marking an auto-created event wrong on the dashboard.
    """
    body = build_event_body(summary, description, deadline, has_time)
    service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()


def delete_event(service, event_id: str, calendar_id: str = "primary") -> None:
    """Remove an already-created event — the "remove from calendar" side of
    marking an auto-created event wrong on the dashboard.

    Idempotent: if the event is already gone (deleted directly in Calendar, or a retry after a
    previous delete that succeeded on Google's side but whose response was lost), the API answers
    404 or 410 ("Resource has been deleted") instead of succeeding again. Remove's actual goal —
    no such event exists — is already true either way, so that is treated as success rather than
    left to bubble up as a 500 that leaves the database row (and so the dashboard) never catching
    up: a real incident (2026-09-22) where two already-deleted duplicate events made every Remove
    click on their rows crash and the rows stayed listed forever.
    """
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as e:
        if e.resp.status not in (404, 410):
            raise
