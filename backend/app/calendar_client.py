"""Google Calendar event creation (Step 4).

Shares OAuth with the Gmail client — see app/google_auth.py.
"""

from __future__ import annotations

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


def create_event(
    service,
    summary: str,
    description: str,
    deadline: datetime,
    has_time: bool,
    calendar_id: str = "primary",
    recurrence_rule: str | None = None,
) -> str:
    """Create the event and return its Calendar event id (stored in
    ProcessedEmail.calendar_event_id for the audit trail / idempotency).
    For a recurring series, this id is the master event's id — patch/delete
    against it affects the whole series, not a single instance.
    """
    body = build_event_body(summary, description, deadline, has_time, recurrence_rule)
    created = service.events().insert(calendarId=calendar_id, body=body).execute()
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
