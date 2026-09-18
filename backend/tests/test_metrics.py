"""Observability layer tests. Needs real Postgres (see conftest.py) —
weekly_correction_rate uses date_trunc, which behaves the same in
production as it does here, unlike the claim logic's ON CONFLICT (which
just plain doesn't exist in SQLite).
"""

from datetime import datetime, timedelta, timezone

from app import metrics
from app.db import repository
from app.db.models import ProcessedEmail, RunStatus
from app.schemas import ExtractionResult


def _extraction(email_id="e1", event_name="Test event", confidence="high"):
    return ExtractionResult(
        email_id=email_id, event_name=event_name, deadline_date_raw="Sep 20",
        deadline_date=datetime.now(timezone.utc) + timedelta(days=5),
        source_context="test", confidence=confidence, action_type="deadline",
    )


def _run(db_session, **overrides):
    run = repository.start_run(db_session)
    kwargs = dict(
        status=RunStatus.SUCCESS, emails_fetched=10, emails_processed=2,
        emails_failed=0, emails_filtered_out=6, emails_already_terminal=2,
    )
    kwargs.update(overrides)
    repository.finish_run(db_session, run.id, **kwargs)
    return db_session.get(type(run), run.id)


class TestFilterPassRate:
    def test_computed_from_fetched_filtered_and_terminal(self, db_session):
        run = _run(db_session, emails_fetched=10, emails_filtered_out=6, emails_already_terminal=2)
        # evaluated = 10 - 2 = 8; passed = 8 - 6 = 2; rate = 2/8 = 0.25
        rows = metrics.run_history(db_session, limit=1)
        assert rows[0]["filter_pass_rate"] == 0.25

    def test_none_when_nothing_was_evaluated(self, db_session):
        """Every fetched email was already terminal -- the filter never
        ran on any of them, so there's no rate to report."""
        run = _run(db_session, emails_fetched=5, emails_filtered_out=0, emails_already_terminal=5)

        rows = metrics.run_history(db_session, limit=1)

        assert rows[0]["filter_pass_rate"] is None

    def test_sent_to_llm_is_processed_plus_failed(self, db_session):
        run = _run(db_session, emails_processed=3, emails_failed=2)

        rows = metrics.run_history(db_session, limit=1)

        assert rows[0]["sent_to_llm"] == 5


class TestAnomalyFlag:
    def test_flags_a_sharp_deviation_from_the_trailing_average(self, db_session):
        # 5 consistent runs at ~90% pass rate, then one that craters to 10%
        for _ in range(5):
            _run(db_session, emails_fetched=20, emails_filtered_out=2, emails_already_terminal=0)  # 90%
        _run(db_session, emails_fetched=20, emails_filtered_out=18, emails_already_terminal=0)  # 10%

        rows = metrics.run_history(db_session, limit=10)

        assert rows[0]["anomaly"] is True  # newest-first, so index 0 is the cratered run

    def test_does_not_flag_when_consistent(self, db_session):
        for _ in range(6):
            _run(db_session, emails_fetched=20, emails_filtered_out=2, emails_already_terminal=0)

        rows = metrics.run_history(db_session, limit=10)

        assert all(r["anomaly"] is False for r in rows)

    def test_small_runs_are_never_flagged(self, db_session):
        """A 1-email run passing or failing is 0%/100% by pure chance, not
        a signal -- FILTER_ANOMALY_MIN_SAMPLE_SIZE guards against this."""
        for _ in range(5):
            _run(db_session, emails_fetched=20, emails_filtered_out=2, emails_already_terminal=0)
        _run(db_session, emails_fetched=1, emails_filtered_out=1, emails_already_terminal=0)

        rows = metrics.run_history(db_session, limit=10)

        assert rows[0]["anomaly"] is False


class TestOutcomeCounts:
    def test_breaks_down_deadlines_review_and_action_items(self, db_session):
        run = repository.start_run(db_session)

        # deadline with a live event -> auto-created
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="cal-1")

        # deadline with no event -> sitting in the review queue
        repository.try_claim_email(db_session, "e2", "t2", "s2")
        repository.mark_completed(db_session, "e2", _extraction(email_id="e2", confidence="low"), calendar_event_id=None)

        # needs_reply, not a duplicate fold -> surfaced as a new action item
        repository.try_claim_email(db_session, "e3", "t3", "s3")
        needs_reply = ExtractionResult(
            email_id="e3", event_name="Interview scheduling", deadline_date_raw=None,
            deadline_date=None, source_context="what times work?", confidence="low", action_type="needs_reply",
        )
        repository.mark_completed(db_session, "e3", needs_reply, calendar_event_id=None)

        # a second needs_reply folded into e3 -> must NOT count as surfaced
        repository.try_claim_email(db_session, "e4", "t4", "s4")
        repository.fold_action_item(db_session, "e3", "e4", "just checking in")
        repository.mark_completed(db_session, "e4", needs_reply, calendar_event_id=None, duplicate_of_email_id="e3")

        repository.finish_run(
            db_session, run.id, status=RunStatus.SUCCESS,
            emails_fetched=4, emails_processed=4, emails_failed=0,
        )

        rows = metrics.run_history(db_session, limit=1)

        assert rows[0]["deadlines_auto_created"] == 1
        assert rows[0]["review_queue_items"] == 1
        assert rows[0]["action_items_surfaced"] == 1  # e4 excluded, folded into e3

    def test_failed_rows_are_excluded_from_every_bucket(self, db_session):
        run = repository.start_run(db_session)
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_failed(db_session, "e1", "some error")
        repository.finish_run(
            db_session, run.id, status=RunStatus.SUCCESS,
            emails_fetched=1, emails_processed=0, emails_failed=1,
        )

        rows = metrics.run_history(db_session, limit=1)

        assert rows[0]["deadlines_auto_created"] == 0
        assert rows[0]["review_queue_items"] == 0
        assert rows[0]["action_items_surfaced"] == 0


class TestWeeklyCorrectionRate:
    def test_buckets_votes_by_week(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="c1")
        repository.set_correction(db_session, "e1", True)

        repository.try_claim_email(db_session, "e2", "t2", "s2")
        repository.mark_completed(db_session, "e2", _extraction(email_id="e2"), calendar_event_id="c2")
        repository.set_correction(db_session, "e2", False)

        weeks = metrics.weekly_correction_rate(db_session)

        assert len(weeks) == 1
        assert weeks[0]["total"] == 2
        assert weeks[0]["correct"] == 1
        assert weeks[0]["rate"] == 0.5

    def test_unvoted_rows_are_excluded(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")
        repository.mark_completed(db_session, "e1", _extraction(email_id="e1"), calendar_event_id="c1")

        weeks = metrics.weekly_correction_rate(db_session)

        assert weeks == []


class _FixedPacificNow(datetime):
    """Freezes metrics.datetime.now() at 2026-09-18 10:00 Pacific."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 18, 10, 0, tzinfo=tz)


def _run_started_at(db_session, started_at, processed, failed):
    run = repository.start_run(db_session)
    repository.finish_run(
        db_session, run.id, status=RunStatus.SUCCESS,
        emails_fetched=processed + failed, emails_processed=processed, emails_failed=failed,
    )
    row = db_session.get(type(run), run.id)
    row.started_at = started_at
    db_session.commit()


class TestDailyLlmUsage:
    def test_sums_processed_plus_failed_across_todays_runs(self, db_session):
        _run(db_session, emails_processed=3, emails_failed=1)  # 4 calls
        _run(db_session, emails_processed=2, emails_failed=0)  # 2 calls

        usage = metrics.daily_llm_usage(db_session)

        assert usage["calls"] == 6
        assert usage["quota"] > 0
        assert usage["pct"] == usage["calls"] / usage["quota"] * 100

    def test_excludes_runs_from_before_today(self, db_session):
        _run(db_session, emails_processed=2, emails_failed=0)  # today
        _run_started_at(db_session, datetime.now(timezone.utc) - timedelta(days=2), processed=9, failed=9)

        assert metrics.daily_llm_usage(db_session)["calls"] == 2

    def test_day_boundary_is_midnight_pacific_not_utc(self, db_session, monkeypatch):
        """Frozen "now" is 10:00 PT on Sep 18. A run at 23:30 PT on Sep 17
        is 06:30 UTC on Sep 18 — same UTC date as now, but still *yesterday*
        for Gemini's quota, so it must not count. A run at 00:30 PT Sep 18
        (07:30 UTC) is today and must.
        """
        monkeypatch.setattr(metrics, "datetime", _FixedPacificNow)
        _run_started_at(db_session, datetime(2026, 9, 18, 6, 30, tzinfo=timezone.utc), processed=5, failed=0)  # 23:30 PT Sep 17
        _run_started_at(db_session, datetime(2026, 9, 18, 7, 30, tzinfo=timezone.utc), processed=2, failed=0)  # 00:30 PT Sep 18

        usage = metrics.daily_llm_usage(db_session)

        assert usage["calls"] == 2
        assert usage["resets_at"] == datetime(2026, 9, 19, 0, 0, tzinfo=metrics.QUOTA_RESET_TZ)

    def test_month_total_includes_earlier_days_but_daily_does_not(self, db_session, monkeypatch):
        monkeypatch.setattr(metrics, "datetime", _FixedPacificNow)
        _run_started_at(db_session, datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc), processed=7, failed=0)  # earlier this month
        _run_started_at(db_session, datetime(2026, 9, 18, 17, 0, tzinfo=timezone.utc), processed=2, failed=0)  # today

        usage = metrics.daily_llm_usage(db_session)

        assert usage["calls"] == 2
        assert usage["calls_this_month"] == 9

    def test_voting_on_an_old_row_no_longer_inflates_usage(self, db_session):
        """Regression for the updated_at problem: this count used to come
        from ProcessedEmail.attempt_count bucketed by updated_at, so touching
        an old row (a vote) pulled its attempts into today.
        """
        repository.try_claim_email(db_session, "old", "t1", "s1")
        repository.mark_completed(db_session, "old", _extraction(email_id="old"), calendar_event_id="c1")
        _run_started_at(db_session, datetime.now(timezone.utc) - timedelta(days=3), processed=1, failed=0)

        repository.set_correction(db_session, "old", True)  # bumps the row's updated_at to now

        assert metrics.daily_llm_usage(db_session)["calls"] == 0

    def test_warning_flags_when_pct_crosses_threshold(self, db_session, monkeypatch):
        monkeypatch.setattr(metrics, "GEMINI_DAILY_QUOTA", 10)
        monkeypatch.setattr(metrics, "LLM_USAGE_WARNING_THRESHOLD_PCT", 80)
        _run(db_session, emails_processed=9, emails_failed=0)  # 9 / 10 = 90%

        usage = metrics.daily_llm_usage(db_session)

        assert usage["pct"] == 90.0
        assert usage["warning"] is True

    def test_no_warning_below_threshold(self, db_session, monkeypatch):
        monkeypatch.setattr(metrics, "GEMINI_DAILY_QUOTA", 100)
        monkeypatch.setattr(metrics, "LLM_USAGE_WARNING_THRESHOLD_PCT", 80)
        _run(db_session, emails_processed=1, emails_failed=0)  # 1%

        assert metrics.daily_llm_usage(db_session)["warning"] is False
