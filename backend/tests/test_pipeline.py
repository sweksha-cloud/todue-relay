import pytest
from sqlalchemy import func, select

from app import pipeline
from app.db import repository
from app.db.models import PipelineRun, ProcessedEmail, ProcessingStatus, RunStatus
from app.gmail_client import EmailMessage
from datetime import datetime, timedelta, timezone

from app.llm_client import LLMRateLimitError, LLMTransientError
from app.pipeline import _claim_and_process
from app.schemas import ExtractionResult


def _email(**overrides) -> EmailMessage:
    defaults = dict(
        id="e1", thread_id="t1", subject="Assignment 3 due Friday",
        sender="prof@school.edu", date="", snippet="",
        body_text="Assignment 3 is due by 5pm on Friday. Please submit by then.",
    )
    defaults.update(overrides)
    return EmailMessage(**defaults)


class TestClaimAndProcessSkipsCalendarInvites:
    def test_calendar_invite_is_skipped_before_ever_claiming(self):
        """A real .ics invite — Gmail/Calendar already surfaces this
        natively, independent of this pipeline. Must never reach
        try_claim_email (session=None here; a DB call would raise).
        """
        email = _email(has_calendar_invite=True)

        result = _claim_and_process(session=None, email=email)

        assert result == "skipped"

    def test_non_invite_email_still_reaches_the_claim_step(self):
        """Sanity check the test setup itself: a plain actionable-shaped
        email is NOT short-circuited by the invite check, and does try to
        reach the (here, deliberately broken) claim step — proves the
        invite-skip test above is actually exercising the early-return
        path, not just always returning 'skipped' for unrelated reasons.
        """
        email = _email(has_calendar_invite=False)

        try:
            _claim_and_process(session=None, email=email)
            raised = False
        except AttributeError:
            raised = True

        assert raised is True


class TestClaimAndProcessFilterOutcome:
    def test_pre_filter_reject_returns_distinct_outcome(self):
        """Observability layer (2026-09-18): the pre-filter reject case
        must return its own distinct value ("filtered_out"), not the
        generic "skipped" also used for calendar invites / claimed
        elsewhere — run_pipeline tallies these separately so a batch of
        invites doesn't get mistaken for a filter regression.
        """
        email = _email(subject="hi", body_text="just saying hello, nothing time-sensitive here")

        result = _claim_and_process(session=None, email=email)

        assert result == "filtered_out"


class TestRateLimitDoesNotCountTowardRetryCap:
    def test_429_is_recorded_as_failed_but_the_attempt_is_refunded(self, db_session, monkeypatch):
        def rate_limited(email):
            raise LLMRateLimitError("429 RESOURCE_EXHAUSTED quotaValue 20")

        monkeypatch.setattr(pipeline, "extract_deadline", rate_limited)

        result = pipeline._claim_and_process(db_session, _email())

        assert result == "failed"
        row = db_session.get(ProcessedEmail, "e1")
        assert row.status == ProcessingStatus.FAILED
        assert "quotaValue" in row.error_message  # still visible on the dashboard
        assert row.attempt_count == 0  # not counted toward MAX_ATTEMPTS_PER_EMAIL

    def test_repeated_429s_never_park_the_email(self, db_session, monkeypatch):
        """A quota outage lasting longer than MAX_ATTEMPTS_PER_EMAIL runs
        (the daily quota resets at midnight Pacific) must not exhaust the cap.
        """
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)

        def rate_limited(email):
            raise LLMRateLimitError("429")

        monkeypatch.setattr(pipeline, "extract_deadline", rate_limited)

        for _ in range(6):  # twice the cap
            assert pipeline._claim_and_process(db_session, _email()) == "failed"

    def test_an_ordinary_failure_still_counts(self, db_session, monkeypatch):
        """Counterpart: a non-429 error is the email's problem and must
        still count, or the retry cap would never trigger at all.
        """
        def boom(email):
            raise ValueError("bad response")

        monkeypatch.setattr(pipeline, "extract_deadline", boom)

        pipeline._claim_and_process(db_session, _email())

        assert db_session.get(ProcessedEmail, "e1").attempt_count == 1


class TestTransientErrorsDoNotCountTowardRetryCap:
    """A Gemini 503 ("high demand") is not the email's fault. On 2026-09-19 to 09-21 such errors used up the
    3 attempts of three good emails and parked them for good. They are refunded now, within a time bound."""

    def _fail_with(self, monkeypatch, exc):
        def failing(email):
            raise exc

        monkeypatch.setattr(pipeline, "extract_deadline", failing)

    def test_a_503_is_recorded_as_failed_but_the_attempt_is_refunded(self, db_session, monkeypatch):
        self._fail_with(monkeypatch, LLMTransientError("503 UNAVAILABLE. This model is currently experiencing high demand."))

        assert pipeline._claim_and_process(db_session, _email()) == "failed"

        row = db_session.get(ProcessedEmail, "e1")
        assert row.status == ProcessingStatus.FAILED
        assert "high demand" in row.error_message  # still visible on the dashboard
        assert row.error_message.startswith("LLMTransientError:")
        assert row.attempt_count == 0

    def test_a_long_outage_never_parks_a_recent_email(self, db_session, monkeypatch):
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)
        self._fail_with(monkeypatch, LLMTransientError("503"))

        for _ in range(8):  # far more than the cap: hourly runs through a long outage
            assert pipeline._claim_and_process(db_session, _email()) == "failed"

        assert repository.would_claim_email(db_session, "e1")  # still retryable

    def test_a_success_after_the_outage_completes_normally(self, db_session, monkeypatch):
        self._fail_with(monkeypatch, LLMTransientError("503"))
        for _ in range(5):
            pipeline._claim_and_process(db_session, _email())

        assert repository.try_claim_email(db_session, "e1", "t1", "s") is True  # the next run can claim it

    def test_an_email_that_keeps_failing_for_days_is_eventually_parked(self, db_session, monkeypatch):
        """The bound: a permanent server error must not be retried every hour forever, because the
        recovery sweep re-fetches FAILED rows by id whatever the fetch window."""
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 3)
        self._fail_with(monkeypatch, LLMTransientError("500 INTERNAL"))
        pipeline._claim_and_process(db_session, _email())  # creates the row, refunded
        row = db_session.get(ProcessedEmail, "e1")
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=repository.TRANSIENT_RETRY_WINDOW_HOURS + 1)
        db_session.commit()

        results = [pipeline._claim_and_process(db_session, _email()) for _ in range(5)]

        assert results[:3] == ["failed", "failed", "failed"]  # counted now, so the cap takes over
        assert results[3:] == ["skipped", "skipped"]  # parked: no longer claimed
        assert db_session.get(ProcessedEmail, "e1").attempt_count == 3

    def test_a_429_is_still_covered(self, db_session, monkeypatch):
        self._fail_with(monkeypatch, LLMRateLimitError("429"))

        pipeline._claim_and_process(db_session, _email())

        assert db_session.get(ProcessedEmail, "e1").attempt_count == 0


# --- Daily call budget guard -------------------------------------------------
# Drives the real run_pipeline() loop against real Postgres; only Gmail and
# Gemini are stubbed. Names are far apart on purpose so action-item duplicate
# detection (name similarity >= 0.7) never folds one test email into another.
_EVENT_NAMES = {"e1": "alpha review", "e2": "zebra invoice", "e3": "quartz meeting", "e4": "hydra summit"}


class _Harness:
    def __init__(self):
        self.emails = []
        self.extract_calls = []
        self.recovery_fetches = []
        self.fail_ids = set()


@pytest.fixture
def harness(db_session, monkeypatch):
    h = _Harness()
    monkeypatch.setattr(pipeline, "get_session", lambda: db_session)
    monkeypatch.setattr(pipeline, "get_gmail_service", lambda: object())
    monkeypatch.setattr(pipeline, "fetch_recent_messages", lambda service, **kw: list(h.emails))
    monkeypatch.setattr(
        pipeline, "fetch_messages_by_ids", lambda service, ids: h.recovery_fetches.append(list(ids)) or []
    )

    def fake_extract(email):
        h.extract_calls.append(email.id)
        if email.id in h.fail_ids:
            raise ValueError("bad response")
        return ExtractionResult(
            email_id=email.id, event_name=_EVENT_NAMES[email.id], deadline_date_raw=None, deadline_date=None,
            source_context="ctx", confidence="low", action_type="needs_reply",
        )

    monkeypatch.setattr(pipeline, "extract_deadline", fake_extract)
    # budget = quota - reserve = 10 - 2 = 8 calls/day
    monkeypatch.setattr(pipeline, "GEMINI_DAILY_QUOTA", 10)
    monkeypatch.setattr(pipeline, "GEMINI_DAILY_RESERVE", 2)
    return h


def _spend(db_session, calls):
    """A finished run earlier today that already made `calls` Gemini calls."""
    run = repository.start_run(db_session)
    repository.finish_run(
        db_session, run.id, status=RunStatus.SUCCESS,
        emails_fetched=calls, emails_processed=calls, emails_failed=0,
    )


class TestAlertWhenAnEmailIsParked:
    """Runs report SUCCESS whenever they complete, so a run that fails on the same emails hour after hour
    looked healthy: three good emails sat parked for days. The pipeline now emails when one is parked."""

    @pytest.fixture(autouse=True)
    def _two_attempts_and_a_captured_alert(self, monkeypatch):
        monkeypatch.setattr(repository, "MAX_ATTEMPTS_PER_EMAIL", 2)
        self.sent = []
        monkeypatch.setattr(pipeline.alerts, "notify_parked", lambda parked, **kw: self.sent.append(parked))

    def test_no_alert_while_an_email_still_has_attempts_left(self, harness):
        harness.fail_ids = {"e1"}
        harness.emails = [_email(id="e1")]

        result = pipeline.run_pipeline()  # attempt 1 of 2

        assert result["failed"] == 1 and result["newly_parked"] == []
        assert self.sent == []

    def test_one_alert_when_the_last_attempt_fails_and_none_after(self, harness):
        harness.fail_ids = {"e1"}
        harness.emails = [_email(id="e1")]
        pipeline.run_pipeline()  # attempt 1

        second = pipeline.run_pipeline()  # attempt 2: parked now
        third = pipeline.run_pipeline()  # no longer claimed, so no second alert

        assert second["newly_parked"] == ["e1"]
        assert len(self.sent) == 1
        assert self.sent[0][0]["id"] == "e1" and "bad response" in self.sent[0][0]["error"]
        assert third["newly_parked"] == [] and len(self.sent) == 1

    def test_everything_parked_in_one_run_is_one_alert(self, harness):
        harness.fail_ids = {"e1", "e2"}
        harness.emails = [_email(id="e1"), _email(id="e2")]
        pipeline.run_pipeline()

        pipeline.run_pipeline()

        assert len(self.sent) == 1 and {p["id"] for p in self.sent[0]} == {"e1", "e2"}

    def test_a_transient_failure_never_parks_an_email_so_never_alerts(self, harness, monkeypatch):
        def outage(email):
            raise LLMTransientError("503 UNAVAILABLE")

        monkeypatch.setattr(pipeline, "extract_deadline", outage)
        harness.emails = [_email(id="e1")]

        for _ in range(6):
            pipeline.run_pipeline()

        assert self.sent == []

    def test_a_dry_run_never_alerts(self, harness, db_session):
        harness.fail_ids = {"e1"}
        harness.emails = [_email(id="e1")]

        pipeline.run_pipeline(dry_run=True)

        assert self.sent == []

    def test_a_problem_sending_the_alert_does_not_fail_the_run(self, harness, db_session, monkeypatch):
        def broken(parked, **kw):
            raise RuntimeError("SNS is down")

        monkeypatch.setattr(pipeline.alerts, "notify_parked", broken)
        harness.fail_ids = {"e1"}
        harness.emails = [_email(id="e1")]
        pipeline.run_pipeline()

        result = pipeline.run_pipeline()  # parks it; the alert blows up

        assert result["newly_parked"] == ["e1"]
        assert repository.get_latest_run(db_session).status == RunStatus.SUCCESS


class TestDailyBudgetGuard:
    def test_stops_claiming_once_the_budget_is_spent(self, harness, db_session):
        _spend(db_session, 6)  # 8 budget - 6 already spent = 2 left
        harness.emails = [_email(id=i) for i in ("e1", "e2", "e3", "e4")]

        result = pipeline.run_pipeline()

        assert result["processed"] == 2
        assert result["deferred"] == 2
        assert harness.extract_calls == ["e1", "e2"]
        # Deferred emails were never claimed — no trace, so a later run sees them fresh.
        claimed = {r.email_id for r in db_session.query(ProcessedEmail).all()}
        assert claimed == {"e1", "e2"}

    def test_the_deferred_count_is_recorded_on_the_run(self, harness, db_session):
        """Deferred mail leaves no other trace (it's never claimed), so the run
        row is the only place that can say "mail is waiting".
        """
        _spend(db_session, 6)  # 2 left
        harness.emails = [_email(id=i) for i in ("e1", "e2", "e3", "e4")]

        pipeline.run_pipeline()

        db_session.expire_all()
        assert repository.get_latest_run(db_session).emails_deferred == 2

    def test_deferred_emails_are_processed_once_budget_frees(self, harness, db_session, monkeypatch):
        _spend(db_session, 6)
        harness.emails = [_email(id=i) for i in ("e1", "e2", "e3", "e4")]
        pipeline.run_pipeline()

        monkeypatch.setattr(pipeline, "GEMINI_DAILY_QUOTA", 50)  # e.g. the next day / a raised quota
        result = pipeline.run_pipeline()

        assert result["already_terminal"] == 2
        assert result["processed"] == 2
        assert result["deferred"] == 0
        assert sorted(harness.extract_calls) == ["e1", "e2", "e3", "e4"]  # nothing lost, nothing repeated

    def test_exactly_enough_budget_processes_everything(self, harness, db_session):
        _spend(db_session, 6)  # 2 left
        harness.emails = [_email(id="e1"), _email(id="e2")]

        result = pipeline.run_pipeline()

        assert result["processed"] == 2
        assert result["deferred"] == 0

    def test_a_failed_call_still_spends_budget(self, harness, db_session):
        _spend(db_session, 6)  # 2 left
        harness.fail_ids = {"e1"}
        harness.emails = [_email(id=i) for i in ("e1", "e2", "e3")]

        result = pipeline.run_pipeline()

        assert (result["failed"], result["processed"], result["deferred"]) == (1, 1, 1)

    def test_emails_that_cost_nothing_are_not_deferred(self, harness, db_session):
        """Budget fully spent: a pre-filter reject and a calendar invite make
        no Gemini call, so they keep their own outcome — only an email that
        would have cost a call is deferred.
        """
        _spend(db_session, 8)  # 0 left
        harness.emails = [
            _email(id="e1", subject="hi", body_text="just saying hello, nothing time-sensitive here"),
            _email(id="e2", has_calendar_invite=True),
            _email(id="e3"),
        ]

        result = pipeline.run_pipeline()

        assert result["filtered_out"] == 1
        assert result["deferred"] == 1
        assert result["processed"] == 0

    def test_recovery_sweep_is_skipped_when_budget_is_spent(self, harness, db_session):
        repository.try_claim_email(db_session, "stuck", "t", "s")
        repository.mark_failed(db_session, "stuck", "503")
        _spend(db_session, 8)  # 0 left

        pipeline.run_pipeline()

        assert harness.recovery_fetches == []  # not even fetched from Gmail

    def test_recovery_sweep_still_runs_with_budget_left(self, harness, db_session):
        """Counterpart — proves the skip above is caused by the budget, not
        by the sweep never running in this harness.
        """
        repository.try_claim_email(db_session, "stuck", "t", "s")
        repository.mark_failed(db_session, "stuck", "503")

        pipeline.run_pipeline()

        assert harness.recovery_fetches == [["stuck"]]

    def test_quota_zero_turns_the_guard_off(self, harness, db_session, monkeypatch):
        monkeypatch.setattr(pipeline, "GEMINI_DAILY_QUOTA", 0)
        _spend(db_session, 500)
        harness.emails = [_email(id=i) for i in ("e1", "e2", "e3", "e4")]

        result = pipeline.run_pipeline()

        assert result["processed"] == 4
        assert result["deferred"] == 0


class TestDryRun:
    """run_pipeline(dry_run=True): report what a real run would do, spending
    nothing and writing nothing (docs/design-decisions.md, decision 13).
    """

    def _snapshot(self, db_session):
        db_session.expire_all()
        return (
            db_session.query(PipelineRun).count(),
            sorted((r.email_id, r.status.value, r.attempt_count) for r in db_session.query(ProcessedEmail).all()),
        )

    def test_makes_no_gemini_calls_and_writes_nothing(self, harness, db_session):
        harness.emails = [_email(id=i) for i in ("e1", "e2", "e3")]
        before = self._snapshot(db_session)

        result = pipeline.run_pipeline(dry_run=True)

        assert harness.extract_calls == []  # zero Gemini calls
        assert self._snapshot(db_session) == before  # no claims, no run row
        assert result["dry_run"] is True
        assert result["would_process"] == 3
        assert result["would_process_ids"] == ["e1", "e2", "e3"]
        assert result["processed"] == result["failed"] == 0

    def test_a_real_run_afterwards_is_unaffected(self, harness, db_session):
        """The dry run must leave no trace that changes what a real run does."""
        harness.emails = [_email(id=i) for i in ("e1", "e2")]
        pipeline.run_pipeline(dry_run=True)

        result = pipeline.run_pipeline()

        assert result["processed"] == 2
        assert harness.extract_calls == ["e1", "e2"]

    def test_respects_the_daily_budget_using_would_be_calls(self, harness, db_session):
        _spend(db_session, 6)  # 2 left
        harness.emails = [_email(id=i) for i in ("e1", "e2", "e3", "e4")]

        result = pipeline.run_pipeline(dry_run=True)

        assert result["would_process"] == 2
        assert result["deferred"] == 2

    def test_classifies_skips_the_same_way_a_real_run_does(self, harness, db_session):
        """Same input, same tallies for everything that costs nothing."""
        repository.try_claim_email(db_session, "e4", "t", "s")
        repository.mark_completed(
            db_session, "e4",
            ExtractionResult(
                email_id="e4", event_name="already done", deadline_date_raw=None, deadline_date=None,
                source_context="c", confidence="low", action_type="needs_reply",
            ),
            calendar_event_id=None,
        )
        harness.emails = [
            _email(id="e1", subject="hi", body_text="just saying hello, nothing time-sensitive here"),  # pre-filter
            _email(id="e2", has_calendar_invite=True),  # invite
            _email(id="e3"),  # would be processed
            _email(id="e4"),  # already done
        ]

        dry = pipeline.run_pipeline(dry_run=True)
        real = pipeline.run_pipeline()

        assert dry["filtered_out"] == real["filtered_out"] == 1
        assert dry["already_terminal"] == real["already_terminal"] == 1
        assert dry["would_process"] == real["processed"] == 1

    def test_a_claim_the_real_run_would_refuse_is_reported_as_a_skip(self, harness, db_session):
        repository.try_claim_email(db_session, "e1", "t", "s")  # live PROCESSING elsewhere
        harness.emails = [_email(id="e1"), _email(id="e2")]

        result = pipeline.run_pipeline(dry_run=True)

        assert result["would_process_ids"] == ["e2"]

    def test_lists_recovery_candidates_without_touching_them(self, harness, db_session):
        repository.try_claim_email(db_session, "stuck", "t", "s")
        repository.mark_failed(db_session, "stuck", "503")
        before = self._snapshot(db_session)

        result = pipeline.run_pipeline(dry_run=True)

        assert result["recovery_candidate_ids"] == ["stuck"]
        assert harness.recovery_fetches == [["stuck"]]  # Gmail is read...
        assert self._snapshot(db_session) == before  # ...the row is not retried or modified


# --- Single-flight guard -------------------------------------------------------
# A real run exits immediately if another run is in progress; a dry run never
# does. Drives the real run_pipeline() against real Postgres (see the harness above).
class TestSingleFlight:
    def _run_count(self, db_session) -> int:
        db_session.rollback()
        return db_session.execute(select(func.count()).select_from(PipelineRun)).scalar_one()

    def test_a_run_in_progress_makes_the_next_real_run_skip_without_touching_anything(self, db_session, harness):
        harness.emails = [_email(id="e1")]
        assert repository.try_start_run(db_session, ttl_minutes=30) is not None  # another live run

        result = pipeline.run_pipeline()

        assert result["skipped_run"] is True
        assert result["fetched"] == 0 and result["processed"] == 0
        assert harness.extract_calls == []  # never reached Gemini
        assert self._run_count(db_session) == 1  # and wrote no second run row

    def test_a_dry_run_ignores_the_guard(self, db_session, harness):
        assert repository.try_start_run(db_session, ttl_minutes=30) is not None

        result = pipeline.run_pipeline(dry_run=True)

        assert result["skipped_run"] is False
        assert result["dry_run"] is True

    def test_a_normal_run_reports_it_was_not_skipped_and_releases_the_guard(self, db_session, harness):
        first = pipeline.run_pipeline()
        second = pipeline.run_pipeline()

        assert first["skipped_run"] is False
        assert second["skipped_run"] is False  # the first finished, so it no longer blocks
        assert self._run_count(db_session) == 2

    def test_a_run_that_errors_out_still_releases_the_guard(self, db_session, harness, monkeypatch):
        def gmail_down():
            raise RuntimeError("gmail down")

        monkeypatch.setattr(pipeline, "get_gmail_service", gmail_down)
        with pytest.raises(RuntimeError):
            pipeline.run_pipeline()

        monkeypatch.setattr(pipeline, "get_gmail_service", lambda: object())
        assert pipeline.run_pipeline()["skipped_run"] is False
