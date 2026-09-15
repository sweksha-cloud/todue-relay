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


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


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

        headers = msg.get("payload", {}).get("headers", [])
        body_text = _decode_body(msg.get("payload", {})) or msg.get("snippet", "")

        messages.append(
            EmailMessage(
                id=msg["id"],
                thread_id=msg["threadId"],
                subject=_header(headers, "Subject"),
                sender=_header(headers, "From"),
                date=_header(headers, "Date"),
                snippet=msg.get("snippet", ""),
                body_text=body_text,
            )
        )

    return messages
