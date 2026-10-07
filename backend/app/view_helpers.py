"""Presentation-only helpers for the Step 7 dashboard templates."""

from __future__ import annotations

from urllib.parse import quote

from app.config import GMAIL_ACCOUNT_EMAIL
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


def gmail_url(thread_id: str) -> str:
    """A link that opens the email's conversation in Gmail on the web, where it can be read or replied to.
    /mail/u/<address>/ picks the right account when several are signed in; /mail/u/0/ is the first one."""
    account = quote(GMAIL_ACCOUNT_EMAIL, safe="@") if GMAIL_ACCOUNT_EMAIL else "0"
    return f"https://mail.google.com/mail/u/{account}/#all/{quote(thread_id, safe='')}"


def friendly_error(message: str | None) -> str:
    """A short, plain label for a recorded error; the full text stays in the tooltip.
    429 is this project's own quota running out; 503 is Google's servers being busy for everyone."""
    if not message:
        return "No error recorded"
    if "429" in message or "RESOURCE_EXHAUSTED" in message:
        return "Rate limit hit (Gemini quota spent)"
    if "503" in message or "UNAVAILABLE" in message:
        return "Gemini overloaded (high demand, temporary)"
    return message
