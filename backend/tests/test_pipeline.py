from app import pipeline
from app.db import repository
from app.db.models import ProcessedEmail, ProcessingStatus
from app.gmail_client import EmailMessage
from app.llm_client import LLMRateLimitError
from app.pipeline import _claim_and_process


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
        """Sanity check the test setup itself: a plain deadline-shaped
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
