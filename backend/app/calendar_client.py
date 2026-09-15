"""Google Calendar event creation (Step 4).

Shares OAuth with the Gmail client — see app/google_auth.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

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
) -> dict:
    """A timed deadline becomes a short block (EVENT_DURATION_MINUTES) at
    that time; a date-only deadline becomes a single all-day event.
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
        return {
            "summary": summary,
            "description": description,
            "start": {"dateTime": aware_deadline.isoformat(), "timeZone": tz},
            "end": {"dateTime": end.isoformat(), "timeZone": tz},
        }

    start_date = deadline.date()
    end_date = start_date + timedelta(days=1)  # Calendar all-day events use an exclusive end date
    return {
        "summary": summary,
        "description": description,
        "start": {"date": start_date.isoformat()},
        "end": {"date": end_date.isoformat()},
    }


def create_event(
    service,
    summary: str,
    description: str,
    deadline: datetime,
    has_time: bool,
    calendar_id: str = "primary",
) -> str:
    """Create the event and return its Calendar event id (stored in
    ProcessedEmail.calendar_event_id for the audit trail / idempotency).
    """
    body = build_event_body(summary, description, deadline, has_time)
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
    """
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
