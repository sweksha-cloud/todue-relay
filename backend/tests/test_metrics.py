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


class TestMonthlyLlmUsage:
    def test_sums_attempt_count_for_rows_touched_this_month(self, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "s1")  # attempt_count=1
        repository.try_claim_email(db_session, "e2", "t2", "s2")  # attempt_count=1
        db_session.execute(
            ProcessedEmail.__table__.update().where(ProcessedEmail.email_id == "e2").values(
                status="failed", claimed_at=datetime.now(timezone.utc) - timedelta(minutes=10)
            )
        )
        repository.try_claim_email(db_session, "e2", "t2", "s2")  # reclaimed -> attempt_count=2

        usage = metrics.monthly_llm_usage(db_session)

        assert usage["calls"] == 3  # 1 (e1) + 2 (e2)
        assert usage["quota"] > 0
        assert usage["pct"] == usage["calls"] / usage["quota"] * 100
