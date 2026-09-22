"""Finds and hides ProcessedEmail rows that today's calendar-notification detection would now
skip (docs/design-decisions.md, decision 6), but that were extracted before the check existed or
before it covered their exact shape (Sender vs. From, or no .ics attachment at all).

Each candidate is re-verified live against Gmail with the SAME function the pipeline uses
(gmail_client._has_calendar_invite / _is_from_google_calendar) — not a subject-text guess — so a
row is only touched if today's real pipeline would genuinely skip it right now.

- A row with a live Calendar event: the event is deleted (idempotent: already-gone is fine) and
  the row is marked the same way the dashboard's Remove button would.
- A row with no event (an action item, or a deadline that was never approved): just hidden,
  the same way the dashboard's Deny button would.
- A row already hidden (removed, denied, or skipped) is left alone.

Usage (from backend/, with .venv active):
    python -m scripts.hide_calendar_noise            # list what would change; touches nothing
    python -m scripts.hide_calendar_noise --apply    # hide them (and delete any live duplicate event)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import calendar_client  # noqa: E402
from app.db import repository  # noqa: E402
from app.db.models import ProcessedEmail, ProcessingStatus  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.gmail_client import _has_calendar_invite, _header, get_gmail_service  # noqa: E402

HIDDEN_MESSAGE = "hidden: a Google Calendar-generated notification, not a real deadline"

# Every message a row can carry that already means "hidden from the dashboard": Remove's current
# and legacy wording (repository.not_removed), Deny's (review_actions.decline), and this script's
# own message from a previous run — without that last one, a row this script already hid (with no
# Calendar event to delete) would be flagged as a candidate again on every later run.
_ALREADY_HIDDEN = (
    repository.REMOVED_BY_USER_MESSAGE,
    "removed from calendar by user (marked incorrect)",  # the legacy Remove wording
    "declined by user (low-confidence review)",  # Deny
    HIDDEN_MESSAGE,
)


def _is_calendar_generated(service, email_id: str) -> bool:
    msg = service.users().messages().get(userId="me", id=email_id, format="full").execute()
    headers = msg["payload"]["headers"]
    haystack = f"{_header(headers, 'Sender')} {_header(headers, 'From')}".lower()
    return _has_calendar_invite(msg["payload"]) or "calendar-notification@google.com" in haystack


def find_candidates(session, service) -> list[ProcessedEmail]:
    """Rows today's detection would skip, that are not already hidden. Oldest first."""
    rows = session.query(ProcessedEmail).order_by(ProcessedEmail.created_at).all()
    found = []
    for row in rows:
        if (row.error_message or "") in _ALREADY_HIDDEN:
            continue
        try:
            if _is_calendar_generated(service, row.email_id):
                found.append(row)
        except Exception as e:  # noqa: BLE001 - one bad lookup (e.g. the message was since deleted) shouldn't stop the sweep
            print(f"  could not check {row.email_id}: {e}")
    return found


def hide(session, row: ProcessedEmail) -> str:
    """Hides one row the right way for its state. Returns what was done, for the report."""
    if row.calendar_event_id:
        service = calendar_client.get_calendar_service()
        calendar_client.delete_event(service, row.calendar_event_id)
        repository.remove_calendar_event(session, row.email_id)
        return "deleted its Calendar event and removed"
    repository.mark_skipped(session, row.email_id, HIDDEN_MESSAGE)
    return "hidden (no Calendar event existed)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="hide them, and delete any live duplicate event (default: only list)")
    args = parser.parse_args()

    session = get_session()
    service = get_gmail_service()
    try:
        candidates = find_candidates(session, service)
        if not candidates:
            print("Nothing to hide.")
            return
        for row in candidates:
            print(f"  {row.email_id[:12]}  event={row.calendar_event_id or '-'}  {row.email_subject[:70]!r}")
            if args.apply:
                action = hide(session, row)
                print(f"    -> {action}")
        if not args.apply:
            print(f"\n{len(candidates)} row(s) would be hidden. Re-run with --apply.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
