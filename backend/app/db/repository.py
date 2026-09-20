"""Crash-safe claim/complete/fail operations for ProcessedEmail.

The claim step is an atomic UPSERT: a fresh email_id inserts a new row; a
retry of a FAILED row, or a PROCESSING row whose claim went stale (worker
crashed mid-batch), reclaims it; anything COMPLETED, SKIPPED, or actively
PROCESSING elsewhere is left untouched. This is what makes a restart safe —
it can re-run the same batch of email ids and only the ones that actually
need work get claimed.
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import BigInteger, func, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD, MAX_ATTEMPTS_PER_EMAIL, STALE_CLAIM_MINUTES
from app.date_utils import has_explicit_time
from app.db.models import ActionType, Confidence, OAuthToken, PipelineRun, ProcessedEmail, ProcessingStatus, RunStatus
from app.schemas import ExtractionResult


def get_oauth_token(session: Session, key: str) -> str | None:
    row = session.get(OAuthToken, key)
    return row.token_json if row else None


def save_oauth_token(session: Session, key: str, token_json: str) -> None:
    stmt = pg_insert(OAuthToken).values(key=key, token_json=token_json)
    stmt = stmt.on_conflict_do_update(
        index_elements=[OAuthToken.key],
        set_={"token_json": token_json},
    )
    session.execute(stmt)
    session.commit()


def try_claim_email(session: Session, email_id: str, thread_id: str, email_subject: str) -> bool:
    """Attempt to claim an email for processing. Returns True if this call
    now owns it (safe to call the LLM), False if it's already terminal
    (completed/skipped), being worked on by a still-live attempt, or FAILED
    and out of retries (attempt_count >= MAX_ATTEMPTS_PER_EMAIL).

    The retry cap applies only to the FAILED branch. A stale PROCESSING
    claim (worker hard-crashed) is still always reclaimable: if it then
    fails, it lands in FAILED with its incremented attempt_count and the
    cap takes over from there.

    This issues a raw UPSERT, bypassing the ORM unit-of-work — if the
    caller already holds a Python ProcessedEmail object for this email_id
    from earlier in the same session, that object's attributes (e.g.
    attempt_count) will be stale until re-fetched with session.refresh().
    """
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=STALE_CLAIM_MINUTES)

    insert_stmt = pg_insert(ProcessedEmail).values(
        email_id=email_id,
        thread_id=thread_id,
        email_subject=email_subject,
        status=ProcessingStatus.PROCESSING,
        attempt_count=1,
        claimed_at=now,
    )
    claim_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ProcessedEmail.email_id],
        set_={
            "status": ProcessingStatus.PROCESSING,
            "claimed_at": now,
            "attempt_count": ProcessedEmail.attempt_count + 1,
        },
        where=(
            (
                (ProcessedEmail.status == ProcessingStatus.FAILED)
                & (ProcessedEmail.attempt_count < MAX_ATTEMPTS_PER_EMAIL)
            )
            | (
                (ProcessedEmail.status == ProcessingStatus.PROCESSING)
                & (ProcessedEmail.claimed_at < stale_before)
            )
        ),
    ).returning(ProcessedEmail.email_id)

    claimed_id = session.execute(claim_stmt).scalar()
    session.commit()
    return claimed_id is not None


def would_claim_email(session: Session, email_id: str) -> bool:
    """Read-only answer to "would try_claim_email succeed right now?" — used
    by dry-run mode (pipeline.run_pipeline(dry_run=True)), which must not
    claim anything. A Python mirror of that function's conditional-upsert
    WHERE clause, so the two can drift: tests/test_repository.py checks this
    against the real try_claim_email across every row state — keep them in step.
    """
    row = session.get(ProcessedEmail, email_id, populate_existing=True)
    if row is None:
        return True  # a fresh email would insert a new claim
    if row.status == ProcessingStatus.FAILED:
        return row.attempt_count < MAX_ATTEMPTS_PER_EMAIL
    if row.status == ProcessingStatus.PROCESSING:
        stale_before = datetime.now(timezone.utc) - timedelta(minutes=STALE_CLAIM_MINUTES)
        return row.claimed_at is not None and row.claimed_at < stale_before
    return False  # COMPLETED / SKIPPED are terminal


def mark_completed(
    session: Session,
    email_id: str,
    extraction: ExtractionResult,
    calendar_event_id: str | None,
    is_implausible: bool = False,
    duplicate_of_email_id: str | None = None,
    date_changed_from: datetime | None = None,
) -> None:
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No claimed row for {email_id}; call try_claim_email first")

    row.status = ProcessingStatus.COMPLETED
    row.completed_at = datetime.now(timezone.utc)
    row.extraction_event_name = extraction.event_name
    row.extraction_deadline_raw = extraction.deadline_date_raw
    row.extraction_deadline_parsed = extraction.deadline_date
    row.extraction_source_context = extraction.source_context
    row.extraction_confidence = Confidence(extraction.confidence)
    row.extraction_action_type = ActionType(extraction.action_type)
    row.extraction_has_time = has_explicit_time(extraction.deadline_date_raw)
    row.extraction_is_recurring = extraction.is_recurring
    row.extraction_recurrence_rule = extraction.recurrence_rule
    row.calendar_event_id = calendar_event_id
    row.is_implausible_date = is_implausible
    row.duplicate_of_email_id = duplicate_of_email_id
    row.date_changed_from = date_changed_from
    session.commit()


def _normalize_event_name(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def find_duplicate_deadline(
    session: Session, event_name: str, exclude_email_id: str, same_day: "date | None" = None
) -> ProcessedEmail | None:
    """Look for an already-tracked, still-live deadline with a
    similar-sounding event name (claude/tradeoffs/duplicate-deadline-detection.md).

    Cheap heuristic on purpose: normalized-name similarity via difflib, no
    embeddings/LLM call. Only matches against rows that still have a live
    Calendar event and haven't already been superseded themselves — a
    duplicate should always resolve to the current, still-relevant row, not
    a stale link in a chain.

    same_day, when given, restricts candidates to that exact calendar day —
    used for the always-on "is this a plain repeat" check. Omit it to
    search across all tracked deadlines regardless of date — used only when
    the email contains reschedule-signaling language
    (filters.contains_reschedule_language), since that search is what finds
    an existing deadline whose date is *different* from this one.
    """
    conditions = [
        ProcessedEmail.status == ProcessingStatus.COMPLETED,
        ProcessedEmail.extraction_action_type == ActionType.DEADLINE,
        ProcessedEmail.calendar_event_id.is_not(None),
        ProcessedEmail.is_stale.is_(False),
        ProcessedEmail.email_id != exclude_email_id,
    ]
    if same_day is not None:
        conditions.append(func.date(ProcessedEmail.extraction_deadline_parsed) == same_day)

    candidates = session.execute(select(ProcessedEmail).where(*conditions)).scalars().all()
    return _best_name_match(candidates, event_name)


def _best_name_match(candidates: list[ProcessedEmail], event_name: str) -> ProcessedEmail | None:
    """Shared by find_duplicate_deadline and find_duplicate_action_item:
    the single best normalized-name match among candidates, if it clears
    DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD.
    """
    target = _normalize_event_name(event_name)
    best_match: ProcessedEmail | None = None
    best_score = 0.0
    for row in candidates:
        if not row.extraction_event_name:
            continue
        score = difflib.SequenceMatcher(
            None, target, _normalize_event_name(row.extraction_event_name)
        ).ratio()
        if score > best_score:
            best_score = score
            best_match = row

    if best_match is not None and best_score >= DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD:
        return best_match
    return None


def find_duplicate_action_item(
    session: Session, event_name: str, exclude_email_id: str
) -> ProcessedEmail | None:
    """Look for an already-tracked needs_reply/unclear action item with a
    similar-sounding name — the dateless counterpart to
    find_duplicate_deadline (claude/post-prod/duplicate-deadline-detection.md).

    No date to restrict the search by, so this always searches every
    tracked action item, unbounded — same "cheap at this project's scale"
    reasoning as find_duplicate_deadline's unbounded lookback. Only matches
    against rows that aren't themselves already folded into an earlier one
    (duplicate_of_email_id is null) — a new follow-up should always fold
    onto the original, current entry, not a stale link in a chain.
    """
    candidates = (
        session.execute(
            select(ProcessedEmail).where(
                ProcessedEmail.status == ProcessingStatus.COMPLETED,
                ProcessedEmail.extraction_action_type.in_([ActionType.NEEDS_REPLY, ActionType.UNCLEAR]),
                ProcessedEmail.duplicate_of_email_id.is_(None),
                ProcessedEmail.email_id != exclude_email_id,
            )
        )
        .scalars()
        .all()
    )
    return _best_name_match(candidates, event_name)


def fold_action_item(session: Session, old_email_id: str, new_email_id: str, new_source_context: str) -> None:
    """Fold a follow-up email into an already-tracked action item instead of
    it becoming a second dashboard entry — append the new context (keeps
    the full audit trail, same principle as everywhere else in this
    project) and let `updated_at`'s auto-bump (see ProcessedEmail) act as
    the "last mentioned" timestamp list_action_items sorts by, so a
    still-live action item resurfaces instead of going stale and silent.
    """
    row = session.get(ProcessedEmail, old_email_id)
    if row is None:
        raise ValueError(f"No such email {old_email_id}")

    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    row.extraction_source_context = f"{row.extraction_source_context}\n\nFollow-up ({today}): {new_source_context}"
    session.commit()


def mark_superseded(session: Session, old_email_id: str, new_email_id: str) -> None:
    """The OLD side of a detected deadline change: flag it stale and point
    at the row that replaced it, without altering its own historical
    extraction data (the audit trail stays intact — see
    claude/tradeoffs/does-this-need-a-database.md).
    """
    row = session.get(ProcessedEmail, old_email_id)
    if row is None:
        raise ValueError(f"No such email {old_email_id}")

    row.is_stale = True
    row.superseded_by_email_id = new_email_id
    session.commit()


def mark_skipped(session: Session, email_id: str, reason: str) -> None:
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No claimed row for {email_id}; call try_claim_email first")

    row.status = ProcessingStatus.SKIPPED
    row.completed_at = datetime.now(timezone.utc)
    row.error_message = reason
    session.commit()


def mark_failed(session: Session, email_id: str, error: str, *, count_attempt: bool = True) -> None:
    """count_attempt=False refunds the attempt try_claim_email just counted,
    for a failure that isn't the email's fault (an API rate-limit 429) —
    otherwise a quota outage would burn through MAX_ATTEMPTS_PER_EMAIL and
    permanently park good emails, when the daily quota only resets at
    midnight Pacific.
    """
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No claimed row for {email_id}; call try_claim_email first")

    row.status = ProcessingStatus.FAILED
    row.error_message = error
    if not count_attempt:
        row.attempt_count = max(0, row.attempt_count - 1)
    session.commit()


def get_recoverable_stuck_email_ids(session: Session) -> list[str]:
    """Non-terminal rows (PROCESSING or FAILED) that `try_claim_email`
    would happily reclaim *if* their email showed up in a fetch batch again
    — a stale PROCESSING claim (worker crashed mid-email) or a plain FAILED
    row (eligible for retry unconditionally, same as the normal fetch loop
    already does for anything still in the batch).

    Normal recovery relies on exactly that: the email showing up again in a
    future run's fetch, so `try_claim_email` can reclaim it. But a fetch
    batch is always `is:unread newer_than:FETCH_WINDOW_DAYS` — a row whose
    email gets read or ages out of that window before a retry succeeds is
    never fetched again and would otherwise stay stuck forever (PROCESSING)
    or silently never retried (FAILED). This powers a direct-by-id recovery
    sweep (see pipeline.py) that doesn't depend on the email still matching
    that search.

    A FAILED row that's out of retries (attempt_count >= MAX_ATTEMPTS_PER_EMAIL)
    is excluded — try_claim_email would refuse it anyway, so fetching it from
    Gmail every run would be pure wasted work.
    """
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=STALE_CLAIM_MINUTES)
    stmt = select(ProcessedEmail.email_id).where(
        (
            (ProcessedEmail.status == ProcessingStatus.PROCESSING)
            & (ProcessedEmail.claimed_at < stale_before)
        )
        | (
            (ProcessedEmail.status == ProcessingStatus.FAILED)
            & (ProcessedEmail.attempt_count < MAX_ATTEMPTS_PER_EMAIL)
        )
    )
    return list(session.execute(stmt).scalars().all())


def get_terminal_email_ids(session: Session, email_ids: list[str]) -> set[str]:
    """Of the given ids, which are already COMPLETED or SKIPPED (i.e. done
    for good, safe to drop from a batch before even fetching full content)."""
    if not email_ids:
        return set()

    stmt = select(ProcessedEmail.email_id).where(
        ProcessedEmail.email_id.in_(email_ids),
        ProcessedEmail.status.in_([ProcessingStatus.COMPLETED, ProcessingStatus.SKIPPED]),
    )
    return set(session.execute(stmt).scalars().all())


def _not_removed():
    """Rows the user did not remove with the dashboard's Remove button. Removed rows are
    kept in the table on purpose (the pipeline treats SKIPPED as done, so the email is never
    re-added, and the "incorrect" vote still counts toward the correction rate); they are just
    not listed. NULL-safe: most rows have no error_message at all.
    """
    return ProcessedEmail.error_message.is_(None) | (ProcessedEmail.error_message != REMOVED_BY_USER_MESSAGE)


def _recent_emails_filter():
    # DEADLINE (or not-yet-classified) rows only — needs_reply/unclear show
    # up in the action items list instead (see list_action_items), not
    # duplicated here. Shared between the page query and its count so they
    # can never drift out of sync with each other.
    # An action item the user scheduled has a live Calendar event now, so it belongs here
    # (with the Reschedule/Remove controls) instead of in the action-items list.
    return (
        ProcessedEmail.extraction_action_type.is_(None)
        | (ProcessedEmail.extraction_action_type == ActionType.DEADLINE)
        | ProcessedEmail.calendar_event_id.is_not(None)
    ) & _not_removed()


def list_recent_emails(session: Session, limit: int = 25, offset: int = 0) -> list[ProcessedEmail]:
    """One page of processed emails, newest first — Step 6/7's history view."""
    stmt = (
        select(ProcessedEmail)
        .where(_recent_emails_filter())
        .order_by(ProcessedEmail.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(stmt).scalars().all())


def count_recent_emails(session: Session) -> int:
    stmt = select(func.count()).select_from(ProcessedEmail).where(_recent_emails_filter())
    return session.execute(stmt).scalar_one()


def set_calendar_event(session: Session, email_id: str, calendar_event_id: str) -> ProcessedEmail:
    """Record that a low-confidence 'needs review' item was approved and
    its Calendar event created — the row stays COMPLETED, just gains an
    event id (display_status then reads it as 'added').
    """
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No such email {email_id}")

    row.calendar_event_id = calendar_event_id
    session.commit()
    session.refresh(row)
    return row


def set_correction(session: Session, email_id: str, is_correct: bool) -> ProcessedEmail:
    """Step 6's correction endpoint: record the user's correct/incorrect verdict."""
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No such email {email_id}")

    row.user_correction = is_correct
    session.commit()
    session.refresh(row)
    return row


def schedule_action_item(
    session: Session, email_id: str, deadline: datetime, calendar_event_id: str, *, has_time: bool = True
) -> ProcessedEmail:
    """The user picked a date for a dateless action item (needs_reply / unclear): the caller
    has created the Calendar event; this records the date and the event on the row.

    extraction_action_type and completed_at are deliberately left alone, so past run
    history (which groups rows by when they completed and classifies them by action type)
    is not rewritten. The row now has a live event, so the dashboard lists it with the
    other scheduled items and offers Reschedule / Remove like any of them.
    """
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No such email {email_id}")

    row.extraction_deadline_parsed = deadline
    row.extraction_has_time = has_time
    row.calendar_event_id = calendar_event_id
    session.commit()
    session.refresh(row)
    return row


def reschedule_email(
    session: Session,
    email_id: str,
    new_deadline: datetime,
    has_time: bool,
) -> ProcessedEmail:
    """The "reschedule" side of marking an auto-created event wrong: the
    Calendar event itself is patched by the caller (calendar_client.
    update_event) — this just keeps our audit record in sync with it.
    Stays COMPLETED with the same calendar_event_id (same event, corrected
    time), marked as an acknowledged correction.
    """
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No such email {email_id}")
    if not row.calendar_event_id:
        raise ValueError(f"{email_id} has no calendar event to reschedule")

    row.extraction_deadline_parsed = new_deadline
    row.extraction_deadline_raw = new_deadline.isoformat()
    row.extraction_has_time = has_time
    row.user_correction = False
    session.commit()
    session.refresh(row)
    return row


# error_message written by remove_calendar_event. It doubles as the marker that hides a
# removed item from the dashboard lists (see _not_removed): no schema change needed, and
# rows removed before this filter existed already carry the same text.
REMOVED_BY_USER_MESSAGE = "removed from calendar by user (marked incorrect)"


def remove_calendar_event(session: Session, email_id: str) -> ProcessedEmail:
    """The "remove from calendar" side of marking an auto-created event
    wrong: the Calendar event itself is deleted by the caller
    (calendar_client.delete_event) — this records that outcome and, by
    clearing calendar_event_id and moving to SKIPPED (not back to
    COMPLETED-without-an-event), prevents it from ever reappearing as a
    'needs review' item that could be approved into creating the event
    right back.
    """
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No such email {email_id}")

    row.status = ProcessingStatus.SKIPPED
    row.calendar_event_id = None
    row.user_correction = False
    row.error_message = REMOVED_BY_USER_MESSAGE
    row.completed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(row)
    return row


def start_run(session: Session) -> PipelineRun:
    run = PipelineRun(status=RunStatus.RUNNING)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


# Arbitrary constant naming "starting a pipeline run" to Postgres's advisory locks.
RUN_LOCK_KEY = 7_215_930_001


def try_start_run(session: Session, *, ttl_minutes: int) -> PipelineRun | None:
    """Single-flight start: create a RUNNING PipelineRun, unless another run is
    already in progress, in which case return None and change nothing.

    "In progress" means a RUNNING row that started within `ttl_minutes`; an older
    RUNNING row is a crashed run's leftover and is ignored (a lease that expires).

    The check and the insert must be one atomic step: two runs that both see "no
    run in progress" and both insert would defeat the guard. pg_advisory_xact_lock
    serializes exactly that critical section, and, being transaction-scoped, it is
    released on commit/rollback, so it is safe through a transaction-mode pooler
    such as Neon's (a session-level lock would not be). The lock is held only for
    the check-and-insert; "a run is in progress" itself is the RUNNING row.
    """
    session.execute(select(func.pg_advisory_xact_lock(literal(RUN_LOCK_KEY, BigInteger))))
    in_progress = session.execute(
        select(PipelineRun.id)
        .where(
            PipelineRun.status == RunStatus.RUNNING,
            PipelineRun.started_at > func.now() - timedelta(minutes=ttl_minutes),
        )
        .limit(1)
    ).first()
    if in_progress is not None:
        session.rollback()  # releases the advisory lock
        return None
    run = PipelineRun(status=RunStatus.RUNNING)
    session.add(run)
    session.commit()  # releases the lock; the RUNNING row is now visible to everyone
    session.refresh(run)
    return run


def finish_run(
    session: Session,
    run_id: int,
    *,
    status: RunStatus,
    emails_fetched: int,
    emails_processed: int,
    emails_failed: int,
    emails_filtered_out: int = 0,
    emails_already_terminal: int = 0,
    emails_deferred: int = 0,
    error_message: str | None = None,
) -> None:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise ValueError(f"No such run {run_id}")

    run.status = status
    run.finished_at = datetime.now(timezone.utc)
    run.emails_fetched = emails_fetched
    run.emails_processed = emails_processed
    run.emails_failed = emails_failed
    run.emails_filtered_out = emails_filtered_out
    run.emails_already_terminal = emails_already_terminal
    run.emails_deferred = emails_deferred
    run.error_message = error_message
    session.commit()


def get_latest_run(session: Session) -> PipelineRun | None:
    stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(1)
    return session.execute(stmt).scalars().first()


def get_correction_rate(session: Session) -> float | None:
    """Fraction of reviewed extractions marked correct. None if nothing's been reviewed yet."""
    total_stmt = select(func.count()).select_from(ProcessedEmail).where(
        ProcessedEmail.user_correction.is_not(None)
    )
    correct_stmt = select(func.count()).select_from(ProcessedEmail).where(
        ProcessedEmail.user_correction.is_(True)
    )
    total = session.execute(total_stmt).scalar_one()
    if total == 0:
        return None
    correct = session.execute(correct_stmt).scalar_one()
    return correct / total


def _action_items_filter():
    # duplicate_of_email_id is set on a follow-up that got folded into an
    # earlier action item (find_duplicate_action_item/fold_action_item) —
    # excluded here so the fold doesn't still show up as a second entry.
    return (
        ProcessedEmail.extraction_action_type.in_([ActionType.NEEDS_REPLY, ActionType.UNCLEAR])
        & ProcessedEmail.duplicate_of_email_id.is_(None)
        & ProcessedEmail.calendar_event_id.is_(None)  # once scheduled it is no longer "no fixed date"
        & _not_removed()
    )


def list_action_items(session: Session, limit: int = 25, offset: int = 0) -> list[ProcessedEmail]:
    """One page of needs_reply / unclear extractions — no fixed date, so no
    calendar event, but still worth surfacing. Most recently *relevant*
    first (updated_at, which a fold bumps — see fold_action_item — so a
    still-live item resurfaces instead of going stale), not just most
    recently first-seen. The dashboard groups these by that same day (a
    page can split a day across two pages at the boundary — accepted,
    minor UX quirk, not worth the extra complexity of day-aligned paging).
    """
    stmt = (
        select(ProcessedEmail)
        .where(_action_items_filter())
        .order_by(ProcessedEmail.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.execute(stmt).scalars().all())


def count_action_items(session: Session) -> int:
    stmt = select(func.count()).select_from(ProcessedEmail).where(_action_items_filter())
    return session.execute(stmt).scalar_one()


def count_deadlines_caught_since(session: Session, since: datetime) -> int:
    stmt = (
        select(func.count())
        .select_from(ProcessedEmail)
        .where(
            ProcessedEmail.status == ProcessingStatus.COMPLETED,
            ProcessedEmail.extraction_deadline_parsed.is_not(None),
            ProcessedEmail.created_at >= since,
        )
    )
    return session.execute(stmt).scalar_one()
