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
from app.filters import is_deadline_candidate
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
        event_id = calendar_client.create_event(
            service,
            summary=extraction.event_name,
            description=extraction.source_context,
            deadline=extraction.deadline_date,
            has_time=has_explicit_time(extraction.deadline_date_raw),
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
