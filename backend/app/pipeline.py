"""Step 5: the orchestration that ties every piece built so far into one
runnable batch — fetch, filter, claim, extract, route, create/queue,
record. Callable as a function (not tied to any particular runner — Step 8
was explicit that this shouldn't be coupled to GitHub Actions specifically)
so it can be invoked from a script, a scheduler, or later a Lambda handler.

Failure isolation: one email's exception is caught and recorded via
mark_failed without aborting the batch — a bad LLM response, a transient
API error, or a malformed date on one email doesn't stop the rest.

Confidence routing (claude/tradeoffs/confidence-routing.md): high
confidence + a plausible date auto-creates the Calendar event; everything
else (low confidence, or a deadline that fails the plausibility check)
lands as "needs review" for the dashboard's approve/decline checkmarks.
Plausibility bounds are throwaway defaults (see app/config.py) — the real
policy decision is still open.
"""

from __future__ import annotations

import logging

from app import calendar_client
from app.config import (
    FETCH_WINDOW_DAYS,
    MAX_EMAILS_PER_RUN,
    PLAUSIBLE_MAX_FUTURE_DAYS,
    PLAUSIBLE_MAX_PAST_DAYS,
)
from app.date_utils import has_explicit_time, is_plausible
from app.db import repository
from app.db.models import RunStatus
from app.db.session import get_session
from app.filters import contains_reschedule_language, is_deadline_candidate
from app.gmail_client import fetch_recent_messages, get_gmail_service
from app.llm_client import extract_deadline

logger = logging.getLogger(__name__)


def run_pipeline() -> dict:
    """Process up to MAX_EMAILS_PER_RUN recent emails end-to-end. Returns a
    summary dict; always records a PipelineRun row, success or failure.
    """
    session = get_session()
    run = repository.start_run(session)
    fetched = processed = failed = 0

    try:
        service = get_gmail_service()
        messages = fetch_recent_messages(
            service,
            max_results=MAX_EMAILS_PER_RUN,
            # is:unread — only scan mail you haven't already read yourself.
            # Combined with the day window so it never scans years of old
            # unread mail, just recent-and-unread.
            query=f"is:unread newer_than:{FETCH_WINDOW_DAYS}d",
        )
        fetched = len(messages)

        terminal_ids = repository.get_terminal_email_ids(session, [m.id for m in messages])

        for email in messages:
            if email.id in terminal_ids:
                continue  # already completed/skipped in a prior run

            if not is_deadline_candidate(email.subject, email.body_text):
                continue  # pre-filter: never even claimed, cheapest possible skip

            if not repository.try_claim_email(session, email.id, email.thread_id, email.subject):
                continue  # claimed elsewhere, or a live attempt already in flight

            try:
                _process_one(session, email)
                processed += 1
            except Exception as e:  # noqa: BLE001 - intentional: isolate one bad email from the batch
                logger.exception("Failed processing email %s", email.id)
                repository.mark_failed(session, email.id, f"{type(e).__name__}: {e}")
                failed += 1

        repository.finish_run(
            session,
            run.id,
            status=RunStatus.SUCCESS,
            emails_fetched=fetched,
            emails_processed=processed,
            emails_failed=failed,
        )
    except Exception as e:
        logger.exception("Pipeline run failed")
        repository.finish_run(
            session,
            run.id,
            status=RunStatus.FAILURE,
            emails_fetched=fetched,
            emails_processed=processed,
            emails_failed=failed,
            error_message=f"{type(e).__name__}: {e}",
        )
        raise
    finally:
        session.close()

    return {"fetched": fetched, "processed": processed, "failed": failed}


def _process_one(session, email) -> None:
    """Raises on any failure — the caller's except block is the single
    place that records mark_failed and counts it, so a failure is never
    miscounted as processed regardless of which step it came from.
    """
    extraction = extract_deadline(email)  # ExtractionParseError propagates on purpose

    if extraction.action_type != "deadline" or extraction.deadline_date is None:
        # needs_reply / unclear, or a "deadline" the model still left dateless
        # (shouldn't happen per the prompt contract, but handled safely).
        repository.mark_completed(session, email.id, extraction, calendar_event_id=None)
        return

    plausible = is_plausible(
        extraction.deadline_date,
        max_past_days=PLAUSIBLE_MAX_PAST_DAYS,
        max_future_days=PLAUSIBLE_MAX_FUTURE_DAYS,
    )

    if extraction.confidence == "high" and plausible:
        service = calendar_client.get_calendar_service()

        # Check already-tracked deadlines before creating a new event —
        # claude/tradeoffs/duplicate-deadline-detection.md. Two tiers:
        # 1. Always check the same day (catches a plain repeat reminder,
        #    regardless of wording).
        # 2. Only if the email itself signals a reschedule, also search
        #    across every other day (catches the deadline having moved).
        #    Accepted, visible-not-silent gap: a reschedule that just
        #    restates a new date with none of those words will create a
        #    second dashboard entry instead of updating the old one — you'd
        #    see two similar entries and can remove the stray one yourself.
        match = repository.find_duplicate_deadline(
            session, extraction.event_name, email.id, same_day=extraction.deadline_date.date()
        )
        if match is None and contains_reschedule_language(email.subject, email.body_text):
            match = repository.find_duplicate_deadline(session, extraction.event_name, email.id)

        new_has_time = has_explicit_time(extraction.deadline_date_raw)
        same_deadline = match is not None and match.extraction_deadline_parsed.date() == extraction.deadline_date.date()
        if same_deadline and new_has_time and match.extraction_has_time:
            # Both extractions have a real time-of-day, same day — only
            # count it as unchanged if the time matches too. A same-day
            # time change ("5pm" -> "3pm") is a real change, not a repeat,
            # and must still update the event.
            same_deadline = match.extraction_deadline_parsed == extraction.deadline_date
        # If either side has no explicit time, day-match alone decides it —
        # comparing full datetimes there would misfire on a date-only
        # deadline's incidental time-of-day (filled in from whenever the
        # pipeline happened to run, not anything the email said).

        if same_deadline:
            # Same event, same effective deadline — a true repeat. Don't
            # touch the calendar, just link this row to the one that
            # already has the live event.
            repository.mark_completed(
                session, email.id, extraction,
                calendar_event_id=match.calendar_event_id,
                duplicate_of_email_id=match.email_id,
            )
        elif match is not None:
            # Same event, different date and/or time — the deadline moved.
            # Update the existing Calendar event in place
            # (claude/tradeoffs/deadline-changed-policy.md) and flag it on
            # the dashboard so the change is never silent.
            calendar_client.update_event(
                service,
                event_id=match.calendar_event_id,
                summary=extraction.event_name,
                description=extraction.source_context,
                deadline=extraction.deadline_date,
                has_time=has_explicit_time(extraction.deadline_date_raw),
            )
            repository.mark_superseded(session, match.email_id, email.id)
            repository.mark_completed(
                session, email.id, extraction,
                calendar_event_id=match.calendar_event_id,
                duplicate_of_email_id=match.email_id,
                date_changed_from=match.extraction_deadline_parsed,
            )
        else:
            event_id = calendar_client.create_event(
                service,
                summary=extraction.event_name,
                description=extraction.source_context,
                deadline=extraction.deadline_date,
                has_time=has_explicit_time(extraction.deadline_date_raw),
                recurrence_rule=extraction.recurrence_rule,
            )
            repository.mark_completed(session, email.id, extraction, calendar_event_id=event_id)
    else:
        # low confidence, or failed the plausibility check — either way,
        # surfaced as "needs review" for the dashboard's approve/decline.
        # is_implausible is tracked distinctly so the review queue can show
        # *why* (bad date vs. just uncertain) rather than treating both the
        # same — never auto-rejected outright either way.
        repository.mark_completed(
            session, email.id, extraction, calendar_event_id=None, is_implausible=not plausible
        )
