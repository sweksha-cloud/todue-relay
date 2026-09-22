"""Step 5: the orchestration that ties every piece built so far into one
runnable batch — fetch, filter, claim, extract, route, create/queue,
record. Callable as a function (not tied to any particular runner — Step 8
was explicit that this shouldn't be coupled to GitHub Actions specifically)
so it can be invoked from a script, a scheduler, or later a Lambda handler.

Failure isolation: one email's exception is caught and recorded via
mark_failed without aborting the batch — a bad LLM response, a transient
API error, or a malformed date on one email doesn't stop the rest.

Confidence routing (docs/design-decisions.md, decision 9): high
confidence + a plausible date auto-creates the Calendar event; everything
else (low confidence, or a deadline that fails the plausibility check)
lands as "needs review" for the dashboard's approve/decline checkmarks.
Plausibility bounds (PLAUSIBLE_MAX_PAST_DAYS / PLAUSIBLE_MAX_FUTURE_DAYS in
app/config.py) were decided 2026-09-14 — see
docs/design-decisions.md, decision 10.
"""

from __future__ import annotations

import logging

from app import calendar_client, metrics
from app.config import (
    FETCH_WINDOW_DAYS,
    GEMINI_DAILY_QUOTA,
    GEMINI_DAILY_RESERVE,
    MAX_EMAILS_PER_RUN,
    PLAUSIBLE_MAX_FUTURE_DAYS,
    PLAUSIBLE_MAX_PAST_DAYS,
    RUN_LOCK_TTL_MINUTES,
)
from app.date_utils import has_explicit_time, is_plausible
from app.db import repository
from app.db.models import ProcessedEmail, RunStatus
from app.db.session import get_session
from app.filters import contains_reschedule_language, is_actionable_candidate
from app.gmail_client import fetch_messages_by_ids, fetch_recent_messages, get_gmail_service
from app import alerts
from app.llm_client import LLMTransientError, extract_deadline

logger = logging.getLogger(__name__)


def run_pipeline(*, dry_run: bool = False) -> dict:
    """Process up to MAX_EMAILS_PER_RUN recent emails end-to-end. Returns a
    summary dict; always records a PipelineRun row, success or failure.

    Daily call budget (docs/design-decisions.md, decision 12): once
    today's Gemini calls reach GEMINI_DAILY_QUOTA - GEMINI_DAILY_RESERVE, no
    further email is claimed this run — it's "deferred", left untouched so a
    later run (after the midnight-Pacific reset) picks it up while it's still
    unread and inside FETCH_WINDOW_DAYS. Applies to every caller, manual runs
    included. Deferral is logged and returned but not persisted anywhere.

    Single-flight (repository.try_start_run): a real run exits immediately, returning
    "skipped_run": True and touching nothing, if another run is already in progress.
    Dry runs bypass this: they write nothing and spend nothing, so they can overlap.

    dry_run (docs/design-decisions.md, decision 13): report what a real run WOULD
    do — which emails it would send to Gemini, defer, skip, or recover —
    without spending quota or changing anything. Gmail and the database are
    only read; nothing is claimed, no Gemini call is made, no Calendar event
    is touched, and no PipelineRun row is written (so metrics and the daily
    usage count are unaffected). The one incidental write is the OAuth token
    cache, if the token happens to need refreshing — same as any read.
    """
    session = get_session()
    if dry_run:
        run = None
    else:
        run = repository.try_start_run(session, ttl_minutes=RUN_LOCK_TTL_MINUTES)
        if run is None:
            # Single-flight: another run is in progress (see try_start_run). Exit
            # quietly and successfully; the next scheduled run picks up anything left.
            session.close()
            logger.warning(
                "Another pipeline run is in progress (started within the last %d min); skipping this one",
                RUN_LOCK_TTL_MINUTES,
            )
            return _empty_summary(skipped_run=True)
    fetched = processed = failed = filtered_out = already_terminal = deferred = would_process = 0
    would_process_ids: list[str] = []
    stuck_ids: list[str] = []
    newly_parked: list[dict] = []

    def finish(status: RunStatus, error_message: str | None = None) -> None:
        if run is None:  # dry run: nothing is ever recorded
            return
        repository.finish_run(
            session,
            run.id,
            status=status,
            emails_fetched=fetched,
            emails_processed=processed,
            emails_failed=failed,
            emails_filtered_out=filtered_out,
            emails_already_terminal=already_terminal,
            emails_deferred=deferred,
            error_message=error_message,
        )

    try:
        calls_left_at_start = _daily_calls_left(session)

        def over_budget() -> bool:
            # Every claimed email is exactly one Gemini call, tallied as
            # processed or failed — so this run's spend so far is their sum.
            # In a dry run nothing is spent, so the calls it WOULD make stand in.
            spent = processed + failed + would_process
            return calls_left_at_start is not None and calls_left_at_start - spent <= 0

        def handle(email) -> None:
            nonlocal processed, failed, filtered_out, deferred, would_process
            outcome = _claim_and_process(session, email, over_budget=over_budget(), dry_run=dry_run)
            if outcome == "processed":
                processed += 1
            elif outcome == "failed":
                failed += 1
                # An email that fails is only claimed again while it has attempts left, so one that is out of
                # attempts now has just been parked for good: that is worth telling a person about.
                if repository.is_parked(session, email.id):
                    row = session.get(ProcessedEmail, email.id)
                    newly_parked.append({"id": email.id, "subject": email.subject, "error": row.error_message if row else None})
            elif outcome == "filtered_out":
                filtered_out += 1
            elif outcome == "deferred":
                deferred += 1
            elif outcome == "would_process":
                would_process += 1
                would_process_ids.append(email.id)

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
                already_terminal += 1
                continue  # already completed/skipped in a prior run

            handle(email)

        # Recovery sweep: a stuck PROCESSING (worker crashed) or FAILED
        # (eligible for retry) row normally gets picked back up once its
        # email shows up again in the fetch above. But that fetch is always
        # is:unread + FETCH_WINDOW_DAYS — if the email gets read or ages out
        # of that window first, it would never be fetched again and stay
        # stuck (or silently un-retried) forever. Fetch those ids directly
        # by id instead, bypassing the search entirely. Skip anything
        # already handled by the loop above, so a row that's both still in
        # today's fetch batch and freshly re-failed there isn't attempted
        # twice in the same run.
        already_seen_ids = {m.id for m in messages}
        stuck_ids = [
            i for i in repository.get_recoverable_stuck_email_ids(session) if i not in already_seen_ids
        ]
        if stuck_ids and over_budget():
            logger.warning(
                "Daily Gemini budget spent — skipping recovery of %d stuck email(s) until a later run", len(stuck_ids)
            )
        elif stuck_ids:
            logger.info("Recovering %d stuck email(s) outside the normal fetch: %s", len(stuck_ids), stuck_ids)
            for email in fetch_messages_by_ids(service, stuck_ids):
                handle(email)

        if deferred:
            logger.warning(
                "Daily Gemini budget spent (quota %s, reserve %s) — deferred %d email(s) to a later run",
                GEMINI_DAILY_QUOTA, GEMINI_DAILY_RESERVE, deferred,
            )

        if newly_parked and not dry_run:
            try:
                alerts.notify_parked(newly_parked)
            except Exception:  # noqa: BLE001 - a notification problem must never turn a good run into a failed one
                logger.exception("Could not send the parked-email alert")

        finish(RunStatus.SUCCESS)
    except Exception as e:
        logger.exception("Pipeline run failed")
        finish(RunStatus.FAILURE, error_message=f"{type(e).__name__}: {e}")
        raise
    finally:
        session.close()

    return {
        "fetched": fetched,
        "processed": processed,
        "failed": failed,
        "filtered_out": filtered_out,
        "already_terminal": already_terminal,
        "deferred": deferred,
        "dry_run": dry_run,
        "would_process": would_process,
        "would_process_ids": would_process_ids,
        "recovery_candidate_ids": stuck_ids,
        "newly_parked": [p["id"] for p in newly_parked],
        "skipped_run": False,
    }


def _empty_summary(*, skipped_run: bool) -> dict:
    return {
        "fetched": 0,
        "processed": 0,
        "failed": 0,
        "filtered_out": 0,
        "already_terminal": 0,
        "deferred": 0,
        "dry_run": False,
        "would_process": 0,
        "would_process_ids": [],
        "recovery_candidate_ids": [],
        "newly_parked": [],
        "skipped_run": skipped_run,
    }


def _daily_calls_left(session) -> int | None:
    """Calls today's budget still allows as this run starts: (quota minus
    reserve) minus calls recorded by today's finished runs (Pacific day, see
    metrics.daily_llm_usage). None when GEMINI_DAILY_QUOTA is 0 — guard off.
    Can be negative or zero; the caller treats <= 0 as "budget spent".
    """
    if not GEMINI_DAILY_QUOTA:
        return None
    budget = max(0, GEMINI_DAILY_QUOTA - GEMINI_DAILY_RESERVE)
    return budget - metrics.daily_llm_usage(session)["calls"]


def _claim_and_process(session, email, *, over_budget: bool = False, dry_run: bool = False) -> str:
    """Shared by the normal fetch loop and the stale-recovery sweep: filter,
    claim, process, and isolate a failure to just this email.

    Returns "processed", "failed", "filtered_out" (rejected by the
    content-based pre-filter specifically — the caller tallies this
    separately, it's what filter-pass-rate/anomaly tracking cares about),
    "skipped" (a calendar invite, or already claimed/terminal elsewhere —
    deliberate exclusions, not a filter signal), or "deferred" (would have
    cost a Gemini call but the daily budget is spent — never claimed, so a
    later run picks it up untouched; checked after the filter so an email
    that costs nothing is still counted as filtered/skipped, not deferred).

    With dry_run, everything above is decided exactly the same way, but the
    email is never claimed or sent to Gemini: one that would have been comes
    back as "would_process" (or "skipped" if the claim would have been refused
    — see repository.would_claim_email), and nothing is written.
    """
    if email.has_calendar_invite:
        # A real .ics calendar invite — Gmail/Calendar already surfaces
        # this natively (RSVP banner, possibly auto-added to the calendar)
        # independent of this pipeline. Extracting a deadline/action item
        # from it too would create a redundant second entry for something
        # already handled — see gmail_client._has_calendar_invite and
        # docs/design-decisions.md, decision 6. Never even claimed,
        # same cheapest-possible-skip pattern as the pre-filter below.
        # Kept out of "filtered_out" below on purpose: a batch of invites
        # arriving is an unrelated category, not a pre-filter regression
        # signal, and would otherwise confound the anomaly flag.
        return "skipped"

    if not is_actionable_candidate(email.subject, email.body_text):
        return "filtered_out"  # pre-filter: never even claimed, cheapest possible skip

    if over_budget:
        return "deferred"

    if dry_run:
        return "would_process" if repository.would_claim_email(session, email.id) else "skipped"

    if not repository.try_claim_email(session, email.id, email.thread_id, email.subject):
        return "skipped"  # claimed elsewhere, or a live attempt already in flight

    try:
        _process_one(session, email)
        return "processed"
    except Exception as e:  # noqa: BLE001 - intentional: isolate one bad email from the batch
        logger.exception("Failed processing email %s", email.id)
        # A 429, a 5xx or a dropped connection isn't this email's fault — don't let it count toward the
        # retry cap (see repository.mark_failed, which also bounds this by the email's age).
        repository.mark_failed(
            session, email.id, f"{type(e).__name__}: {e}", count_attempt=not isinstance(e, LLMTransientError)
        )
        return "failed"


def _process_one(session, email) -> None:
    """Raises on any failure — the caller's except block is the single
    place that records mark_failed and counts it, so a failure is never
    miscounted as processed regardless of which step it came from.
    """
    extraction = extract_deadline(email)  # ExtractionParseError propagates on purpose

    if extraction.action_type != "deadline" or extraction.deadline_date is None:
        # needs_reply / unclear, or a "deadline" the model still left dateless
        # (shouldn't happen per the prompt contract, but handled safely).
        match = None
        if extraction.action_type in ("needs_reply", "unclear"):
            # A follow-up on something already tracked ("did you see my
            # last email about scheduling?") folds into the existing
            # action item instead of becoming a second dashboard entry —
            # the dateless counterpart to deadline duplicate detection, see
            # docs/design-decisions.md, decision 5.
            match = repository.find_duplicate_action_item(session, extraction.event_name, email.id)

        if match is not None:
            repository.fold_action_item(session, match.email_id, email.id, extraction.source_context)
            repository.mark_completed(
                session, email.id, extraction, calendar_event_id=None, duplicate_of_email_id=match.email_id
            )
        else:
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
        # docs/design-decisions.md, decision 5. Two tiers:
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
            # (docs/design-decisions.md, decision 5) and flag it on
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
