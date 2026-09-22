"""Read-only Gmail authentication and message fetching."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.google_auth import get_google_credentials


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    subject: str
    sender: str
    date: str
    snippet: str
    body_text: str
    already_on_calendar: bool = False


def get_gmail_service():
    """Return an authenticated, read-only Gmail API service.

    Shares OAuth (and its cached token) with the Calendar client — see
    app/google_auth.py — so there's one consent flow for both APIs.
    """
    return build("gmail", "v1", credentials=get_google_credentials())


def _decode_body(payload: dict) -> str:
    """Extract plain-text body from a Gmail message payload, walking multipart parts."""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []) or []:
        text = _decode_body(part)
        if text:
            return text

    # Fall back to snippet-worthy nothing; caller uses msg["snippet"] as backup.
    return ""


_CALENDAR_INVITE_MIME_TYPES = {"text/calendar", "application/ics"}

# The RFC 5322 Sender address Google Calendar's own notification system uses for EVERY automated
# email it sends about an event — distinct from From, which shows the human organizer, so a
# person reading it knows who to reply to. Checked directly against five real, distinct subject
# patterns from this inbox (2026-09-21): "New event: ...", "Canceled event: ...", a real
# "Invitation: ..." (which also has an .ics part), "Updated invitation: ...", and "Accepted:
# ..." (an RSVP confirmation copy) — every one carried this exact Sender, even the three with no
# .ics attachment and no distinguishing subject wording. An earlier version of this check looked
# for the X-Google-Calendar-Notification header instead; it was present on only two of the five
# and missed "Updated invitation:" and "Accepted:" entirely, so it was replaced with this.
#
# Missing this let real bugs through: extraction ran on a "New event" notification and created a
# second, duplicate Calendar event for something Google had already added; a "Canceled event"
# notification (nothing real to act on) was extracted as an "unclear" action item instead of
# being skipped.
_CALENDAR_NOTIFICATION_SENDER = "calendar-notification@google.com"


def _has_calendar_invite(payload: dict) -> bool:
    """A real calendar invite (.ics, MIME type text/calendar or
    application/ics) — checked directly against real inbox data (2026-09-18):
    every such attachment carried METHOD:REQUEST with this account listed as
    an ATTENDEE, whether sent by a person via Google Calendar or
    auto-attached by a third-party tool (Zoom, a registration system, etc).
    That's the exact signal Gmail's own calendar integration uses to show
    the Yes/Maybe/No RSVP banner and (depending on Calendar's auto-add
    setting) add it to the calendar directly — independent of, and before,
    anything this pipeline does. Extracting a deadline/action item from one
    of these too would create a second, redundant entry for something
    already handled — see docs/design-decisions.md, decision 6.
    """
    if payload.get("mimeType") in _CALENDAR_INVITE_MIME_TYPES:
        return True
    filename = payload.get("filename") or ""
    if filename.lower().endswith(".ics"):
        return True
    return any(_has_calendar_invite(part) for part in payload.get("parts", []) or [])


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _is_from_google_calendar(headers: list[dict]) -> bool:
    """See _CALENDAR_NOTIFICATION_SENDER above: true for any automated email Google Calendar
    itself sent about an event, whatever the subject wording and whether or not it has an .ics
    attachment."""
    return _CALENDAR_NOTIFICATION_SENDER in _header(headers, "Sender").lower()


def _to_email_message(msg: dict) -> EmailMessage:
    headers = msg.get("payload", {}).get("headers", [])
    body_text = _decode_body(msg.get("payload", {})) or msg.get("snippet", "")
    already_on_calendar = _has_calendar_invite(msg.get("payload", {})) or _is_from_google_calendar(headers)

    return EmailMessage(
        id=msg["id"],
        thread_id=msg["threadId"],
        subject=_header(headers, "Subject"),
        sender=_header(headers, "From"),
        date=_header(headers, "Date"),
        snippet=msg.get("snippet", ""),
        body_text=body_text,
        already_on_calendar=already_on_calendar,
    )


def fetch_recent_messages(service, max_results: int = 50, query: str | None = None) -> list[EmailMessage]:
    """Fetch the most recent messages from the authenticated inbox.

    `query` accepts standard Gmail search syntax (e.g. "newer_than:7d") so
    callers can bound the fetch window without pulling the whole mailbox.
    """
    list_kwargs = {"userId": "me", "maxResults": max_results}
    if query:
        list_kwargs["q"] = query

    results = service.users().messages().list(**list_kwargs).execute()
    message_stubs = results.get("messages", [])

    messages: list[EmailMessage] = []
    for stub in message_stubs:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="full")
                .execute()
            )
        except HttpError as e:
            # One bad message shouldn't abort the whole fetch.
            print(f"Skipping message {stub['id']}: {e}")
            continue

        messages.append(_to_email_message(msg))

    return messages


def fetch_messages_by_ids(service, message_ids: list[str]) -> list[EmailMessage]:
    """Fetch specific messages directly by id, bypassing any search query
    (unread status, date window, etc). Used to recover a stuck PROCESSING or
    FAILED row whose email no longer matches the normal fetch query — see
    repository.get_recoverable_stuck_email_ids.
    """
    messages: list[EmailMessage] = []
    for message_id in message_ids:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as e:
            # Message may have been deleted since it was claimed — skip it,
            # same failure-isolation policy as the normal fetch path.
            print(f"Skipping message {message_id}: {e}")
            continue

        messages.append(_to_email_message(msg))

    return messages
