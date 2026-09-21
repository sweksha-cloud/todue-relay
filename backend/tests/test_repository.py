"""Tests for the core idempotency guarantee this whole project depends on.
Needs real Postgres (see conftest.py) — the claim logic uses
`ON CONFLICT ... WHERE ... RETURNING`, which has no SQLite equivalent.
"""

from datetime import datetime, timedelta, timezone

from app.db import repository
from app.db.models import PipelineRun, ProcessedEmail, ProcessingStatus, RunStatus
from app.schemas import ExtractionResult


def _extraction(
    email_id="e1", confidence="high", action_type="deadline", deadline=None,
    is_recurring=False, recurrence_rule=None, event_name="Test event", source_context="test",
):
    return ExtractionResult(
        email_id=email_id,
        event_name=event_name,
        deadline_date_raw="Sep 20",
        deadline_date=deadline or datetime.now(timezone.utc) + timedelta(days=5),
        source_context=source_context,
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


def _fail_n_times(db_session, email_id, n):
    """Claim then fail an email n times — leaves it FAILED with attempt_count == n."""
    for _ in range(n):
        assert repository.try_claim_email(db_session, email_id, "t1", "subject") is True
        repository.mark_failed(db_session, email_id, "boom")


class TestRetryCap:
    """A deterministically failing email must stop being retried (and stop
    costing a Gemini call every run) once it hits MAX_ATTEMPTS_PER_EMAIL.
    """

    def test_failed_email_below_cap_is_still_reclaimable(self, db_session, monkeypatch):
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)
        _fail_n_times(db_session, "e1", 2)  # attempt_count == 2, cap is 3

        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is True

    def test_failed_email_at_cap_is_not_reclaimed(self, db_session, monkeypatch):
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)
        _fail_n_times(db_session, "e1", 3)  # attempt_count == 3 == cap

        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is False

        row = db_session.get(ProcessedEmail, "e1")
        db_session.refresh(row)
        assert row.status == ProcessingStatus.FAILED  # parked visibly, not hidden
        assert row.error_message == "boom"
        assert row.attempt_count == 3  # the refused claim didn't increment it

    def test_stale_processing_claim_is_still_reclaimable_past_the_cap(self, db_session, monkeypatch):
        """The cap is FAILED-only: a hard-crashed worker (row left
        PROCESSING) must stay recoverable no matter its attempt_count.
        """
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)
        _fail_n_times(db_session, "e1", 2)
        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is True  # attempt 3, PROCESSING

        row = db_session.get(ProcessedEmail, "e1")
        row.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=999)
        db_session.commit()

        assert repository.try_claim_email(db_session, "e1", "t1", "subject") is True

    def test_recovery_sweep_excludes_failed_rows_at_cap(self, db_session, monkeypatch):
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)
        _fail_n_times(db_session, "retryable", 2)
        _fail_n_times(db_session, "exhausted", 3)

        ids = repository.get_recoverable_stuck_email_ids(db_session)

        assert ids == ["retryable"]


class TestWouldClaimEmail:
    """would_claim_email is a read-only Python mirror of try_claim_email's
    SQL WHERE clause (used by dry-run mode). If the two drift, dry-run would
    report something the real run wouldn't do — so check it against the real
    thing across every row state.
    """

    def _make_states(self, db_session, monkeypatch):
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)
        # fresh: no row at all
        # live_processing: claimed just now
        repository.try_claim_email(db_session, "live_processing", "t", "s")
        # stale_processing: claimed long ago (worker crashed)
        repository.try_claim_email(db_session, "stale_processing", "t", "s")
        row = db_session.get(ProcessedEmail, "stale_processing")
        row.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=999)
        db_session.commit()
        # failed under / at the retry cap
        _fail_n_times(db_session, "failed_under_cap", 2)
        _fail_n_times(db_session, "failed_at_cap", 3)
        # terminal
        repository.try_claim_email(db_session, "completed", "t", "s")
        repository.mark_completed(db_session, "completed", _extraction(email_id="completed"), calendar_event_id="c")
        repository.try_claim_email(db_session, "skipped", "t", "s")
        repository.mark_skipped(db_session, "skipped", "no deadline")

    def test_agrees_with_try_claim_email_in_every_state(self, db_session, monkeypatch):
        self._make_states(db_session, monkeypatch)
        ids = ["fresh", "live_processing", "stale_processing", "failed_under_cap",
               "failed_at_cap", "completed", "skipped"]

        predicted = {i: repository.would_claim_email(db_session, i) for i in ids}
        actual = {i: repository.try_claim_email(db_session, i, "t", "s") for i in ids}

        assert predicted == actual
        # ...and the states genuinely differ, so this isn't vacuously all-True/all-False:
        assert predicted == {
            "fresh": True, "live_processing": False, "stale_processing": True,
            "failed_under_cap": True, "failed_at_cap": False, "completed": False, "skipped": False,
        }

    def test_writes_nothing(self, db_session, monkeypatch):
        self._make_states(db_session, monkeypatch)
        before = {r.email_id: (r.status, r.attempt_count, r.claimed_at) for r in db_session.query(ProcessedEmail).all()}

        for i in list(before) + ["fresh"]:
            repository.would_claim_email(db_session, i)

        after = {r.email_id: (r.status, r.attempt_count, r.claimed_at) for r in db_session.query(ProcessedEmail).all()}
        assert after == before  # no row created for "fresh", none modified


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
        assert row.user_correction is None  # a plain reschedule is not a verdict on the extraction

    def test_remove_clears_event_and_prevents_reapproval(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "subject")
        repository.mark_completed(db_session, "e1", _extraction(), calendar_event_id="cal-1")

        row = repository.remove_calendar_event(db_session, "e1")

        assert row.calendar_event_id is None
        assert row.status == ProcessingStatus.SKIPPED  # not back to "needs review"


class TestFindDuplicateDeadline:
    def test_similar_name_is_matched(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="cal-1")

        match = repository.find_duplicate_deadline(db_session, "Test event", "e2")

        assert match is not None
        assert match.email_id == "e1"

    def test_dissimilar_name_is_not_matched(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="cal-1")

        match = repository.find_duplicate_deadline(db_session, "Completely unrelated club social", "e2")

        assert match is None

    def test_needs_reply_rows_are_not_candidates(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        extraction = _extraction(email_id="e1", action_type="needs_reply")
        repository.mark_completed(db_session, "e1", extraction, calendar_event_id=None)

        match = repository.find_duplicate_deadline(db_session, "Test event", "e2")

        assert match is None

    def test_row_without_a_live_event_is_not_a_candidate(self, db_session):
        """A queued 'needs review' deadline (never approved, no calendar
        event) shouldn't be matched against — nothing to update or link to."""
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id=None)

        match = repository.find_duplicate_deadline(db_session, "Test event", "e2")

        assert match is None

    def test_already_stale_row_is_not_a_candidate(self, db_session):
        """A duplicate should resolve to the current row, not a stale link
        further back in a chain of updates."""
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="cal-1")
        # e2 just needs to exist to satisfy the FK — kept unclaimed-to-COMPLETED
        # on purpose so it isn't itself a candidate, isolating the assertion
        # to "the stale e1 row is excluded."
        repository.try_claim_email(db_session, "e2", "t2", "s2")
        repository.mark_superseded(db_session, "e1", "e2")

        match = repository.find_duplicate_deadline(db_session, "Test event", "e3")

        assert match is None

    def test_excludes_self(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="cal-1")

        match = repository.find_duplicate_deadline(db_session, "Test event", "e1")

        assert match is None

    def test_same_day_restricts_to_that_date(self, db_session):
        tracked_date = datetime.now(timezone.utc) + timedelta(days=5)
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1", _extraction(email_id="e1", deadline=tracked_date), calendar_event_id="cal-1"
        )

        same_day_match = repository.find_duplicate_deadline(
            db_session, "Test event", "e2", same_day=tracked_date.date()
        )
        different_day_match = repository.find_duplicate_deadline(
            db_session, "Test event", "e2", same_day=(tracked_date + timedelta(days=1)).date()
        )

        assert same_day_match is not None and same_day_match.email_id == "e1"
        assert different_day_match is None


class TestMarkSuperseded:
    def test_marks_stale_and_links_to_new_email(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="cal-1")
        repository.try_claim_email(db_session, "e2", "t2", "s2")

        repository.mark_superseded(db_session, "e1", "e2")

        row = db_session.get(ProcessedEmail, "e1")
        assert row.is_stale is True
        assert row.superseded_by_email_id == "e2"
        # historical extraction data is untouched
        assert row.extraction_event_name == "Test event"


class TestMarkCompletedDuplicateFields:
    def test_duplicate_of_and_date_changed_from_are_stored(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="cal-0")
        repository.try_claim_email(db_session, "e2", "t2", "s2")
        old_date = datetime.now(timezone.utc) + timedelta(days=5)
        repository.mark_completed(
            db_session, "e2", _extraction(email_id="e2"),
            calendar_event_id="cal-1", duplicate_of_email_id="e1", date_changed_from=old_date,
        )

        row = db_session.get(ProcessedEmail, "e2")
        assert row.duplicate_of_email_id == "e1"
        assert row.date_changed_from == old_date


class TestFindDuplicateActionItem:
    def test_similar_name_is_matched(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1",
            _extraction(email_id="e1", action_type="needs_reply", event_name="Interview scheduling"),
            calendar_event_id=None,
        )

        match = repository.find_duplicate_action_item(db_session, "Interview scheduling", "e2")

        assert match is not None
        assert match.email_id == "e1"

    def test_dissimilar_name_is_not_matched(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1",
            _extraction(email_id="e1", action_type="needs_reply", event_name="Interview scheduling"),
            calendar_event_id=None,
        )

        match = repository.find_duplicate_action_item(db_session, "Completely unrelated club social", "e2")

        assert match is None

    def test_deadline_rows_are_not_candidates(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1",
            _extraction(email_id="e1", action_type="deadline", event_name="Interview scheduling"),
            calendar_event_id="cal-1",
        )

        match = repository.find_duplicate_action_item(db_session, "Interview scheduling", "e2")

        assert match is None

    def test_already_folded_row_is_not_a_candidate(self, db_session):
        """A new follow-up should fold onto the original, current entry —
        not a stale link further back in a chain of follow-ups."""
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1",
            _extraction(email_id="e1", action_type="needs_reply", event_name="Interview scheduling"),
            calendar_event_id=None,
        )
        repository.try_claim_email(db_session, "e2", "t2", "s2")
        repository.mark_completed(
            db_session, "e2",
            _extraction(email_id="e2", action_type="needs_reply", event_name="Interview scheduling"),
            calendar_event_id=None, duplicate_of_email_id="e1",
        )

        match = repository.find_duplicate_action_item(db_session, "Interview scheduling", "e3")

        assert match is not None
        assert match.email_id == "e1"

    def test_excludes_self(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1",
            _extraction(email_id="e1", action_type="needs_reply", event_name="Interview scheduling"),
            calendar_event_id=None,
        )

        match = repository.find_duplicate_action_item(db_session, "Interview scheduling", "e1")

        assert match is None


class TestFoldActionItem:
    def test_appends_context_and_bumps_updated_at(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1",
            _extraction(
                email_id="e1", action_type="needs_reply", event_name="Interview scheduling",
                source_context="What times work for you?",
            ),
            calendar_event_id=None,
        )
        original_updated_at = db_session.get(ProcessedEmail, "e1").updated_at

        repository.fold_action_item(db_session, "e1", "e2", "Just checking in, did you see my last email?")

        row = db_session.get(ProcessedEmail, "e1")
        assert "What times work for you?" in row.extraction_source_context
        assert "Just checking in, did you see my last email?" in row.extraction_source_context
        assert row.updated_at >= original_updated_at

    def test_folded_row_excluded_from_action_items_list(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(
            db_session, "e1",
            _extraction(email_id="e1", action_type="needs_reply", event_name="Interview scheduling"),
            calendar_event_id=None,
        )
        repository.try_claim_email(db_session, "e2", "t2", "s2")
        repository.fold_action_item(db_session, "e1", "e2", "follow-up")
        repository.mark_completed(
            db_session, "e2",
            _extraction(email_id="e2", action_type="needs_reply", event_name="Interview scheduling"),
            calendar_event_id=None, duplicate_of_email_id="e1",
        )

        items = repository.list_action_items(db_session)

        assert len(items) == 1
        assert items[0].email_id == "e1"


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


class TestFinishRunFilterCounts:
    """Observability layer (2026-09-18): filter pass/fail is never
    persisted anywhere else, so these two counts on PipelineRun are the
    only record of what the pre-filter rejected each run.
    """

    def test_filtered_out_and_already_terminal_are_stored(self, db_session):
        run = repository.start_run(db_session)

        repository.finish_run(
            db_session, run.id, status=RunStatus.SUCCESS,
            emails_fetched=10, emails_processed=3, emails_failed=1,
            emails_filtered_out=5, emails_already_terminal=1,
        )

        row = db_session.get(PipelineRun, run.id)
        assert row.emails_filtered_out == 5
        assert row.emails_already_terminal == 1

    def test_defaults_to_zero_when_omitted(self, db_session):
        """Callers that don't pass these (none currently, but defends
        against a future one) shouldn't leave the row in a null state."""
        run = repository.start_run(db_session)

        repository.finish_run(
            db_session, run.id, status=RunStatus.SUCCESS,
            emails_fetched=1, emails_processed=1, emails_failed=0,
        )

        row = db_session.get(PipelineRun, run.id)
        assert row.emails_filtered_out == 0
        assert row.emails_already_terminal == 0


class TestFinishRunDeferred:
    def test_records_the_deferred_count(self, db_session):
        run = repository.start_run(db_session)
        repository.finish_run(
            db_session, run.id, status=RunStatus.SUCCESS,
            emails_fetched=10, emails_processed=2, emails_failed=0, emails_deferred=4,
        )

        assert db_session.get(PipelineRun, run.id).emails_deferred == 4

    def test_defaults_to_zero(self, db_session):
        run = repository.start_run(db_session)
        repository.finish_run(
            db_session, run.id, status=RunStatus.SUCCESS,
            emails_fetched=1, emails_processed=1, emails_failed=0,
        )

        assert db_session.get(PipelineRun, run.id).emails_deferred == 0


def _failed_row(db, email_id, error, attempts, age_hours=1):
    """A FAILED row with the given error, attempts and age."""
    repository.try_claim_email(db, email_id, f"t-{email_id}", f"Subject {email_id}")
    repository.mark_failed(db, email_id, error)
    row = db.get(ProcessedEmail, email_id)
    row.attempt_count = attempts
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    db.commit()
    return row


class TestTransientRefundIsBoundedByAge:
    def test_a_transient_failure_on_a_recent_email_is_refunded(self, db_session):
        repository.try_claim_email(db_session, "e1", "t", "s")  # attempt 1

        repository.mark_failed(db_session, "e1", "ServerError: 503", count_attempt=False)

        assert db_session.get(ProcessedEmail, "e1").attempt_count == 0

    def test_a_transient_failure_on_an_old_email_counts(self, db_session, monkeypatch):
        monkeypatch.setattr(repository, "TRANSIENT_RETRY_WINDOW_HOURS", 48)
        repository.try_claim_email(db_session, "e1", "t", "s")
        db_session.get(ProcessedEmail, "e1").created_at = datetime.now(timezone.utc) - timedelta(hours=49)
        db_session.commit()

        repository.mark_failed(db_session, "e1", "ServerError: 503", count_attempt=False)

        assert db_session.get(ProcessedEmail, "e1").attempt_count == 1

    def test_the_window_is_configurable(self, db_session, monkeypatch):
        monkeypatch.setattr(repository, "TRANSIENT_RETRY_WINDOW_HOURS", 1)
        repository.try_claim_email(db_session, "e1", "t", "s")
        db_session.get(ProcessedEmail, "e1").created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        db_session.commit()

        repository.mark_failed(db_session, "e1", "x", count_attempt=False)

        assert db_session.get(ProcessedEmail, "e1").attempt_count == 1

    def test_an_ordinary_failure_always_counts_however_young(self, db_session):
        repository.try_claim_email(db_session, "e1", "t", "s")

        repository.mark_failed(db_session, "e1", "ValueError: bad")

        assert db_session.get(ProcessedEmail, "e1").attempt_count == 1


class TestUnparkTransientFailures:
    def test_finds_only_rows_an_outage_exhausted(self, db_session):
        _failed_row(db_session, "outage", "ServerError: 503 UNAVAILABLE. high demand", attempts=3)
        _failed_row(db_session, "typed", "LLMTransientError: 503", attempts=3)
        _failed_row(db_session, "quota", "LLMRateLimitError: 429", attempts=3)
        _failed_row(db_session, "own-fault", "ValueError: bad response", attempts=3)
        _failed_row(db_session, "not-yet-parked", "ServerError: 503", attempts=1)

        found = {r.email_id for r in repository.find_parked_transient_failures(db_session)}

        assert found == {"outage", "typed", "quota"}

    def test_ignores_rows_that_are_not_failed(self, db_session):
        repository.try_claim_email(db_session, "done", "t", "s")
        row = db_session.get(ProcessedEmail, "done")
        row.status, row.attempt_count, row.error_message = ProcessingStatus.COMPLETED, 3, "ServerError: 503"
        db_session.commit()

        assert repository.find_parked_transient_failures(db_session) == []

    def test_unparking_gives_a_fresh_set_of_attempts_and_makes_the_row_claimable_again(self, db_session):
        _failed_row(db_session, "outage", "ServerError: 503", attempts=3)
        assert repository.would_claim_email(db_session, "outage") is False  # parked

        ids = repository.unpark_transient_failures(db_session)

        assert ids == ["outage"]
        assert db_session.get(ProcessedEmail, "outage").attempt_count == 0
        assert repository.would_claim_email(db_session, "outage") is True
        assert repository.try_claim_email(db_session, "outage", "t", "s") is True

    def test_leaves_rows_that_failed_for_their_own_reasons_parked(self, db_session):
        _failed_row(db_session, "own-fault", "ExtractionParseError: bad schema", attempts=3)

        assert repository.unpark_transient_failures(db_session) == []
        assert db_session.get(ProcessedEmail, "own-fault").attempt_count == 3

    def test_keeps_the_error_message_so_the_history_is_not_lost(self, db_session):
        _failed_row(db_session, "outage", "ServerError: 503 UNAVAILABLE", attempts=3)

        repository.unpark_transient_failures(db_session)

        assert db_session.get(ProcessedEmail, "outage").error_message == "ServerError: 503 UNAVAILABLE"
