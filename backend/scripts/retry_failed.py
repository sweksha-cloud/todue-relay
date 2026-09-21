"""Lists, and with --apply retries, emails that a Gemini outage exhausted the retry cap on.

A run that fails on Gemini's "503, high demand" used to count against an email's attempts, so a long enough
outage parked good emails for good (docs/design-decisions.md, decision 25). Such errors no longer count, but
emails parked before that are still parked. This finds the ones that failed *only* for that reason and
gives them a fresh set of attempts; the next hourly run (or the recovery sweep) retries them. Emails that
failed for their own reasons are left alone.

Usage (from backend/, with .venv active):
    python -m scripts.retry_failed            # list what would be retried; changes nothing
    python -m scripts.retry_failed --apply    # give them fresh attempts
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import repository  # noqa: E402
from app.db.session import get_session  # noqa: E402


def run(session, apply: bool) -> list[tuple[str, str, int, str]]:
    """(email id, subject, attempts, error) for each parked-by-an-outage email; un-parks them if apply."""
    rows = repository.find_parked_transient_failures(session)
    found = [(r.email_id, r.email_subject, r.attempt_count, (r.error_message or "")[:100]) for r in rows]
    if apply and rows:
        repository.unpark_transient_failures(session)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="give them fresh attempts (default: only list)")
    args = parser.parse_args()

    session = get_session()
    try:
        found = run(session, args.apply)
    finally:
        session.close()

    if not found:
        print("Nothing parked by a transient error.")
        return
    for email_id, subject, attempts, error in found:
        print(f"  {email_id[:12]}  attempts={attempts}  {subject[:50]!r}\n      {error}")
    print(f"\n{len(found)} email(s) " + ("given fresh attempts." if args.apply else "would be retried. Re-run with --apply."))


if __name__ == "__main__":
    main()
