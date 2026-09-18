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
    has_calendar_invite: bool = False


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
    already handled — see claude/tradeoffs/calendar-invite-emails.md.
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


def _to_email_message(msg: dict) -> EmailMessage:
    headers = msg.get("payload", {}).get("headers", [])
    body_text = _decode_body(msg.get("payload", {})) or msg.get("snippet", "")

    return EmailMessage(
        id=msg["id"],
        thread_id=msg["threadId"],
        subject=_header(headers, "Subject"),
        sender=_header(headers, "From"),
        date=_header(headers, "Date"),
        snippet=msg.get("snippet", ""),
        body_text=body_text,
        has_calendar_invite=_has_calendar_invite(msg.get("payload", {})),
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
    (unread status, date window, etc). Used to recover a stuck PROCESSING
    row whose email no longer matches the normal fetch query — see
    repository.get_stale_processing_email_ids.
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
