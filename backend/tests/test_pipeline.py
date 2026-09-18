from app.gmail_client import EmailMessage
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
