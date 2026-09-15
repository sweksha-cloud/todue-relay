"""Crash-safe claim/complete/fail operations for ProcessedEmail.

The claim step is an atomic UPSERT: a fresh email_id inserts a new row; a
retry of a FAILED row, or a PROCESSING row whose claim went stale (worker
crashed mid-batch), reclaims it; anything COMPLETED, SKIPPED, or actively
PROCESSING elsewhere is left untouched. This is what makes a restart safe —
it can re-run the same batch of email ids and only the ones that actually
need work get claimed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import STALE_CLAIM_MINUTES
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
    (completed/skipped) or being worked on by a still-live attempt.

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
            (ProcessedEmail.status == ProcessingStatus.FAILED)
            | (
                (ProcessedEmail.status == ProcessingStatus.PROCESSING)
                & (ProcessedEmail.claimed_at < stale_before)
            )
        ),
    ).returning(ProcessedEmail.email_id)

    claimed_id = session.execute(claim_stmt).scalar()
    session.commit()
    return claimed_id is not None


def mark_completed(
    session: Session,
    email_id: str,
    extraction: ExtractionResult,
    calendar_event_id: str | None,
    is_implausible: bool = False,
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
    session.commit()


def mark_skipped(session: Session, email_id: str, reason: str) -> None:
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No claimed row for {email_id}; call try_claim_email first")

    row.status = ProcessingStatus.SKIPPED
    row.completed_at = datetime.now(timezone.utc)
    row.error_message = reason
    session.commit()


def mark_failed(session: Session, email_id: str, error: str) -> None:
    row = session.get(ProcessedEmail, email_id)
    if row is None:
        raise ValueError(f"No claimed row for {email_id}; call try_claim_email first")

    row.status = ProcessingStatus.FAILED
    row.error_message = error
    session.commit()


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


def _recent_emails_filter():
    # DEADLINE (or not-yet-classified) rows only — needs_reply/unclear show
    # up in the action items list instead (see list_action_items), not
    # duplicated here. Shared between the page query and its count so they
    # can never drift out of sync with each other.
    return ProcessedEmail.extraction_action_type.is_(None) | (
        ProcessedEmail.extraction_action_type == ActionType.DEADLINE
    )


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
    row.error_message = "removed from calendar by user (marked incorrect)"
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


def finish_run(
    session: Session,
    run_id: int,
    *,
    status: RunStatus,
    emails_fetched: int,
    emails_processed: int,
    emails_failed: int,
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
    return ProcessedEmail.extraction_action_type.in_([ActionType.NEEDS_REPLY, ActionType.UNCLEAR])


def list_action_items(session: Session, limit: int = 25, offset: int = 0) -> list[ProcessedEmail]:
    """One page of needs_reply / unclear extractions — no fixed date, so no
    calendar event, but still worth surfacing. Newest first; the dashboard
    groups these by the day processed (a page can split a day across two
    pages at the boundary — accepted, minor UX quirk, not worth the extra
    complexity of day-aligned paging).
    """
    stmt = (
        select(ProcessedEmail)
        .where(_action_items_filter())
        .order_by(ProcessedEmail.created_at.desc())
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
