"""Tests for the core idempotency guarantee this whole project depends on.
Needs real Postgres (see conftest.py) — the claim logic uses
`ON CONFLICT ... WHERE ... RETURNING`, which has no SQLite equivalent.
"""

from datetime import datetime, timedelta, timezone

from app.db import repository
from app.db.models import ProcessedEmail, ProcessingStatus
from app.schemas import ExtractionResult


def _extraction(
    email_id="e1", confidence="high", action_type="deadline", deadline=None,
    is_recurring=False, recurrence_rule=None,
):
    return ExtractionResult(
        email_id=email_id,
        event_name="Test event",
        deadline_date_raw="Sep 20",
        deadline_date=deadline or datetime.now(timezone.utc) + timedelta(days=5),
        source_context="test",
        confidence=confidence,
        action_type=action_type,
        is_recurring=is_recurring,
        recurrence_rule=recurrence_rule,
    )


class TestTryClaimEmail:
    def test_fresh_email_can_be_claimed(self, db_session):
        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is True

    def test_actively_processing_email_cannot_be_reclaimed(self, db_session):
        """The core race-condition guard: two overlapping runs (or a retry
        of a still-in-flight attempt) must not both think they own it.
        """
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is False

    def test_completed_email_cannot_be_reclaimed(self, db_session):
        """This is the actual idempotency guarantee: a finished email is
        never reprocessed, never re-sent to the LLM, never double-billed.
        """
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(), calendar_event_id="cal-1")

        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is False

    def test_skipped_email_cannot_be_reclaimed(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_skipped(db_session, "e1", "no real deadline")

        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is False

    def test_failed_email_can_be_reclaimed(self, db_session):
        """Failure isolation: a transient error (LLM timeout, API error)
        must be retryable on the next run, not stuck forever.
        """
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_failed(db_session, "e1", "LLM timeout")

        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is True

        row = db_session.get(ProcessedEmail, "e1")
        assert row.attempt_count == 2  # incremented, not reset

    def test_crashed_claim_becomes_reclaimable_after_stale_window(self, db_session):
        """Crash-recovery: if a run dies mid-processing (never reaches
        mark_completed/failed), the claim shouldn't be stuck as
        permanently 'in progress' forever.
        """
        repository.try_claim_email(db_session, "e1", "t1", "subject")

        # Simulate a crash: backdate claimed_at past the stale window.
        row = db_session.get(ProcessedEmail, "e1")
        row.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=999)
        db_session.commit()

        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is True


class TestMarkCompleted:
    def test_high_confidence_deadline_with_event(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(confidence="high"), calendar_event_id="cal-1")

        row = db_session.get(ProcessedEmail, "e1")
        assert row.status == ProcessingStatus.COMPLETED
        assert row.calendar_event_id == "cal-1"
        assert row.is_implausible_date is False

    def test_implausible_flag_is_stored(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(), calendar_event_id=None, is_implausible=True)

        row = db_session.get(ProcessedEmail, "e1")
        assert row.is_implausible_date is True
        assert row.calendar_event_id is None

    def test_recurring_extraction_stores_flag_and_rule(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        extraction = _extraction(is_recurring=True, recurrence_rule="FREQ=MONTHLY;BYMONTHDAY=1")
        repository.mark_completed(db_session, "e1", extraction, calendar_event_id="cal-1")

        row = db_session.get(ProcessedEmail, "e1")
        assert row.extraction_is_recurring is True
        assert row.extraction_recurrence_rule == "FREQ=MONTHLY;BYMONTHDAY=1"

    def test_non_recurring_extraction_defaults_false(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(), calendar_event_id="cal-1")

        row = db_session.get(ProcessedEmail, "e1")
        assert row.extraction_is_recurring is False
        assert row.extraction_recurrence_rule is None


class TestGetTerminalEmailIds:
    def test_only_completed_and_skipped_are_terminal(self, db_session):
        repository.try_claim_email(db_session, "completed", "t1", "s1")
        repository.mark_completed(db_session, "completed", _extraction(email_id="completed"), calendar_event_id=None)

        repository.try_claim_email(db_session, "skipped", "t2", "s2")
        repository.mark_skipped(db_session, "skipped", "no deadline")

        repository.try_claim_email(db_session, "failed", "t3", "s3")
        repository.mark_failed(db_session, "failed", "error")

        repository.try_claim_email(db_session, "processing", "t4", "s4")

        terminal = repository.get_terminal_email_ids(
            db_session, ["completed", "skipped", "failed", "processing", "never-seen"]
        )

        assert terminal == {"completed", "skipped"}

    def test_empty_input_returns_empty_set(self, db_session):
        assert repository.get_terminal_email_ids(db_session, []) == set()


class TestRescheduleAndRemove:
    def test_reschedule_keeps_event_but_updates_date(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(), calendar_event_id="cal-1")

        new_date = datetime.now(timezone.utc) + timedelta(days=10)
        row = repository.reschedule_email(db_session, "e1", new_date, has_time=True)

        assert row.calendar_event_id == "cal-1"  # same event, corrected
        assert row.extraction_deadline_parsed == new_date
        assert row.user_correction is False  # rescheduling implies it was wrong

    def test_remove_clears_event_and_prevents_reapproval(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(), calendar_event_id="cal-1")

        row = repository.remove_calendar_event(db_session, "e1")

        assert row.calendar_event_id is None
        assert row.status == ProcessingStatus.SKIPPED  # not back to "needs review"


class TestActionItemsVsRecentEmails:
    def test_deadline_items_excluded_from_action_items(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(action_type="deadline"), calendar_event_id="cal-1")

        assert repository.list_action_items(db_session) == []
        assert len(repository.list_recent_emails(db_session)) == 1

    def test_needs_reply_excluded_from_recent_emails(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        extraction = ExtractionResult(
            email_id="e1", event_name="Interview scheduling", deadline_date_raw=None,
            deadline_date=None, source_context="reply needed", confidence="low", action_type="needs_reply",
        )
        repository.mark_completed(db_session, "e1", extraction, calendar_event_id=None)

        assert repository.list_recent_emails(db_session) == []
        assert len(repository.list_action_items(db_session)) == 1
