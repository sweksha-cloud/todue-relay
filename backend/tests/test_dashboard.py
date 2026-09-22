"""Renders the real dashboard pages (Jinja templates included) against the test
Postgres. Only the DB dependency is swapped; the app and templates are real.
"""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.db import repository
from app.db.models import ProcessedEmail, ProcessingStatus, RunStatus
from app.schemas import ExtractionResult
from app import calendar_client, date_utils, main, metrics, pipeline
from app.db.session import get_db
from app.main import app


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)  # no `with`: startup events (schema creation) don't run
    app.dependency_overrides.clear()


def _section(html: str, key: str) -> str:
    """The HTML of one category section on the main page (the <details> with id section-<key>), or ''."""
    m = re.search(rf'<details[^>]*id="section-{key}".*?</details>', html, re.S)
    return m.group(0) if m else ""


def _finished_run(db_session, **overrides):
    run = repository.start_run(db_session)
    kwargs = dict(status=RunStatus.SUCCESS, emails_fetched=10, emails_processed=2, emails_failed=0)
    kwargs.update(overrides)
    repository.finish_run(db_session, run.id, **kwargs)


class TestDeferredVisibility:
    def test_main_page_says_mail_is_waiting_when_the_last_run_deferred(self, client, db_session):
        _finished_run(db_session, emails_deferred=3)

        html = client.get("/").text

        assert client.get("/").status_code == 200
        assert "3 deferred — daily Gemini quota spent" in html
        assert "will be picked up after the quota resets" in html

    def test_main_page_is_quiet_when_nothing_was_deferred(self, client, db_session):
        _finished_run(db_session, emails_deferred=0)

        html = client.get("/").text

        assert "deferred — daily Gemini quota spent" not in html

    def test_main_page_still_renders_with_no_runs_at_all(self, client):
        response = client.get("/")

        assert response.status_code == 200
        assert "No runs yet" in response.text

    def test_metrics_page_has_a_deferred_column_showing_the_count(self, client, db_session):
        _finished_run(db_session, emails_deferred=3)

        html = client.get("/metrics").text

        assert "<th>Deferred</th>" in html
        assert "<td>3</td>" in html

    def test_metrics_page_shows_a_dash_for_a_run_that_deferred_nothing(self, client, db_session):
        _finished_run(db_session, emails_deferred=0)

        html = client.get("/metrics").text

        assert "<td>—</td>" in html


def _fake_dry_run(calls, **result):
    """Stands in for run_pipeline; records how it was called."""
    def fake(**kwargs):
        calls.append(kwargs)
        return {"would_process": 0, "deferred": 0, **result}
    return fake


class TestCheckWaitingMail:
    def test_the_button_is_on_the_main_page(self, client):
        assert 'hx-post="/waiting"' in client.get("/").text

    def test_it_only_ever_runs_the_dry_run_path(self, client, monkeypatch):
        """The safety property: a page click must never be a real run, which
        would spend Gemini quota and change pipeline state.
        """
        calls = []
        monkeypatch.setattr(pipeline, "run_pipeline", _fake_dry_run(calls, would_process=1))

        client.post("/waiting")

        assert calls == [{"dry_run": True}]

    def test_reports_waiting_and_held_back_counts(self, client, monkeypatch):
        monkeypatch.setattr(pipeline, "run_pipeline", _fake_dry_run([], would_process=2, deferred=1))

        response = client.post("/waiting")

        assert response.status_code == 200
        assert "<strong>3</strong> waiting" in response.text  # 2 next run + 1 held back
        assert "2 will be sent to Gemini on the next run" in response.text
        assert "1 held back by the daily limit" in response.text

    def test_zero_waiting_is_said_plainly(self, client, monkeypatch):
        monkeypatch.setattr(pipeline, "run_pipeline", _fake_dry_run([]))

        text = client.post("/waiting").text

        assert "<strong>0</strong> waiting" in text
        assert "held back" not in text

    def test_a_failure_is_shown_not_raised(self, client, monkeypatch):
        def broken(**kwargs):
            raise RuntimeError("gmail is down")

        monkeypatch.setattr(pipeline, "run_pipeline", broken)

        response = client.post("/waiting")

        assert response.status_code == 200  # htmx would refuse to swap a 5xx
        assert "Couldn't check" in response.text
        assert "gmail is down" in response.text

    def test_error_text_is_escaped(self, client, monkeypatch):
        def broken(**kwargs):
            raise ValueError("<script>alert(1)</script>")

        monkeypatch.setattr(pipeline, "run_pipeline", broken)

        text = client.post("/waiting").text

        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;" in text

    def test_get_is_not_allowed(self, client):
        """POST-only, so a crawler or link prefetch can't trigger a Gmail read."""
        assert client.get("/waiting").status_code == 405


# --- Removed items ---------------------------------------------------------------------
def _completed_row(db_session, email_id="e1", subject="Nominations due", event_id="cal-1", action_type="deadline"):
    """A finished extraction, with a live Calendar event unless event_id is None."""
    deadline = datetime.now(timezone.utc) + timedelta(days=3)
    repository.try_claim_email(db_session, email_id, f"t-{email_id}", subject)
    repository.mark_completed(
        db_session, email_id,
        ExtractionResult(
            email_id=email_id, event_name=subject, deadline_date_raw="in 3 days",
            deadline_date=deadline if action_type == "deadline" else None,
            source_context="ctx", confidence="high", action_type=action_type,
        ),
        calendar_event_id=event_id,
    )
    return db_session.get(ProcessedEmail, email_id)


@pytest.fixture
def calendar_calls(monkeypatch):
    """Stub the Calendar API: record what the dashboard asks it to do."""
    calls = {"deleted": [], "created": []}
    monkeypatch.setattr(calendar_client, "get_calendar_service", lambda: object())
    monkeypatch.setattr(calendar_client, "delete_event", lambda service, event_id: calls["deleted"].append(event_id))
    return calls


class TestRemovedItemsAreHidden:
    def test_remove_deletes_the_event_and_returns_an_empty_body_so_the_row_disappears(
        self, client, db_session, calendar_calls
    ):
        _completed_row(db_session)

        response = client.post("/emails/e1/remove")

        assert response.status_code == 200
        assert response.text == ""  # htmx swaps the row out of the table
        assert calendar_calls["deleted"] == ["cal-1"]

    def test_a_removed_item_no_longer_appears_on_the_dashboard(self, client, db_session, calendar_calls):
        _completed_row(db_session, "e1", subject="Nominations due")
        _completed_row(db_session, "e2", subject="Rent reminder", event_id="cal-2")
        assert "Nominations due" in client.get("/").text

        client.post("/emails/e1/remove")
        page = client.get("/").text

        assert "Nominations due" not in page
        assert "Rent reminder" in page  # other rows are untouched
        assert [r.email_id for r in repository.list_recent_emails(db_session)] == ["e2"]
        assert repository.count_recent_emails(db_session) == 1

    def test_the_removed_row_stays_in_the_database_so_the_email_is_never_re_added(
        self, client, db_session, calendar_calls
    ):
        _completed_row(db_session)

        client.post("/emails/e1/remove")

        row = db_session.get(ProcessedEmail, "e1")
        assert row is not None
        assert row.status == ProcessingStatus.SKIPPED
        assert row.calendar_event_id is None
        assert "e1" in repository.get_terminal_email_ids(db_session, ["e1"])  # the pipeline won't reprocess it

    def test_a_removal_is_not_an_incorrect_vote(self, client, db_session, calendar_calls):
        """Removing an email means "I don't want this on my calendar", not "the model got it
        wrong". It must not change the correction rate, and it doesn't record a verdict.
        """
        _completed_row(db_session, "e1")
        _completed_row(db_session, "e2", event_id="cal-2", subject="Kept")
        repository.set_correction(db_session, "e2", True)
        assert repository.get_correction_rate(db_session) == 1.0

        client.post("/emails/e1/remove")

        assert repository.get_correction_rate(db_session) == 1.0  # unchanged: the removal is not a vote
        assert db_session.get(ProcessedEmail, "e1").user_correction is None  # no verdict was recorded

    def test_rows_with_no_error_message_or_other_errors_are_still_listed(self, db_session):
        _completed_row(db_session, "e1", subject="Plain row", event_id=None)
        repository.try_claim_email(db_session, "e2", "t2", "Failed row")
        repository.mark_failed(db_session, "e2", "ValueError: something else broke")

        listed = {r.email_id for r in repository.list_recent_emails(db_session)}

        assert listed == {"e1", "e2"}  # only the "removed by user" marker hides a row

    def test_a_removed_action_item_is_hidden_from_the_action_items_list_too(self, db_session):
        row = _completed_row(db_session, "a1", subject="Reply to advisor", event_id=None, action_type="needs_reply")
        assert [r.email_id for r in repository.list_action_items(db_session)] == ["a1"]

        row.error_message = repository.REMOVED_BY_USER_MESSAGE
        db_session.commit()

        assert repository.list_action_items(db_session) == []
        assert repository.count_action_items(db_session) == 0


# --- Scheduling an action item ---------------------------------------------------------
@pytest.fixture
def create_calls(monkeypatch):
    """Stub Calendar creation (and deletion), recording what the dashboard asks for."""
    calls = {"created": [], "deleted": []}

    def fake_create(service, **kwargs):
        calls["created"].append(kwargs)
        return f"cal-new-{len(calls['created'])}"

    monkeypatch.setattr(calendar_client, "get_calendar_service", lambda: object())
    monkeypatch.setattr(calendar_client, "create_event", fake_create)
    monkeypatch.setattr(calendar_client, "delete_event", lambda service, event_id: calls["deleted"].append(event_id))
    monkeypatch.setattr(date_utils, "CALENDAR_TIMEZONE", "America/Los_Angeles")
    return calls


def _action_item(db_session, email_id="a1", subject="Reply to advisor about thesis"):
    return _completed_row(db_session, email_id, subject=subject, event_id=None, action_type="needs_reply")


class TestScheduleActionItem:
    def test_creates_a_calendar_event_at_the_chosen_time_and_records_it(self, client, db_session, create_calls):
        _action_item(db_session)

        response = client.post("/emails/a1/schedule", data={"new_datetime": "2026-10-05T14:30"})

        assert response.status_code == 200
        assert response.text == ""
        assert response.headers["HX-Refresh"] == "true"  # the page reloads so the item moves lists
        expected = datetime(2026, 10, 5, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert create_calls["created"] == [
            dict(summary="Reply to advisor about thesis", description="ctx", deadline=expected, has_time=True)
        ]
        row = db_session.get(ProcessedEmail, "a1")
        assert row.calendar_event_id == "cal-new-1"
        assert row.extraction_deadline_parsed == expected
        assert row.extraction_has_time is True

    def test_the_item_moves_from_action_items_to_the_regular_list_with_reschedule_controls(
        self, client, db_session, create_calls
    ):
        _action_item(db_session)
        before = client.get("/").text
        assert "/emails/a1/schedule" in before  # the Schedule control is offered

        client.post("/emails/a1/schedule", data={"new_datetime": "2026-10-05T14:30"})

        assert repository.list_action_items(db_session) == []
        assert repository.count_action_items(db_session) == 0
        assert [r.email_id for r in repository.list_recent_emails(db_session)] == ["a1"]
        after = client.get("/").text
        assert "/emails/a1/schedule" not in after  # no longer an unscheduled action item
        assert "/emails/a1/reschedule" in after  # it now has the usual scheduled-item controls
        assert "/emails/a1/remove" in after

    def test_past_run_history_is_not_rewritten(self, client, db_session, create_calls):
        row = _action_item(db_session)
        client.post("/emails/a1/schedule", data={"new_datetime": "2026-10-05T14:30"})

        counts = metrics._outcome_counts([db_session.get(ProcessedEmail, "a1")])

        assert counts["action_items_surfaced"] == 1  # still what it was when its run surfaced it
        assert counts["deadlines_auto_created"] == 0

    def test_a_scheduled_item_can_then_be_removed_and_disappears_from_both_lists(
        self, client, db_session, create_calls
    ):
        _action_item(db_session)
        client.post("/emails/a1/schedule", data={"new_datetime": "2026-10-05T14:30"})

        removed = client.post("/emails/a1/remove")

        assert removed.status_code == 200 and removed.text == ""
        assert create_calls["deleted"] == ["cal-new-1"]
        assert repository.list_action_items(db_session) == []
        assert repository.list_recent_emails(db_session) == []

    def test_scheduling_twice_does_not_create_a_second_event(self, client, db_session, create_calls):
        _action_item(db_session)
        client.post("/emails/a1/schedule", data={"new_datetime": "2026-10-05T14:30"})

        again = client.post("/emails/a1/schedule", data={"new_datetime": "2026-10-06T09:00"})

        assert again.status_code == 400
        assert len(create_calls["created"]) == 1

    def test_only_unscheduled_action_items_can_be_scheduled(self, client, db_session, create_calls):
        _completed_row(db_session, "d1", subject="Rent due", event_id="cal-9")  # a normal deadline with an event

        response = client.post("/emails/d1/schedule", data={"new_datetime": "2026-10-05T14:30"})

        assert response.status_code == 400
        assert create_calls["created"] == []

    def test_unknown_email_and_bad_datetime_are_rejected_without_touching_the_calendar(
        self, client, db_session, create_calls
    ):
        _action_item(db_session)

        assert client.post("/emails/nope/schedule", data={"new_datetime": "2026-10-05T14:30"}).status_code == 404
        assert client.post("/emails/a1/schedule", data={"new_datetime": "not-a-date"}).status_code == 400
        assert create_calls["created"] == []
        assert db_session.get(ProcessedEmail, "a1").calendar_event_id is None


# --- Correction-rate statistics ----------------------------------------------------------
def _vote(db_session, email_id, is_correct, subject="Some deadline"):
    _completed_row(db_session, email_id, subject=subject, event_id=None)
    repository.set_correction(db_session, email_id, is_correct)


class TestRemovedItemsAreNotVotes:
    def test_explicit_correct_and_incorrect_votes_still_count(self, db_session):
        _vote(db_session, "e1", True, "One")
        _vote(db_session, "e2", False, "Two")
        _vote(db_session, "e3", True, "Three")

        assert repository.get_correction_rate(db_session) == pytest.approx(2 / 3)
        weeks = metrics.weekly_correction_rate(db_session)
        assert (weeks[-1]["correct"], weeks[-1]["total"]) == (2, 3)

    def test_only_the_legacy_auto_votes_from_the_old_remove_button_are_excluded(self, db_session):
        """Rows removed before Remove stopped recording a verdict hold an automatic "incorrect" vote
        with the old "(marked incorrect)" text; those must not count, with no data migration. A vote the
        user cast explicitly still counts even if the row was removed afterwards.
        """
        _vote(db_session, "e1", True, "Kept and correct")
        _vote(db_session, "e2", False, "Wrong extraction")
        _vote(db_session, "legacy", False, "Removed with the old button")
        db_session.get(ProcessedEmail, "legacy").error_message = "removed from calendar by user (marked incorrect)"
        _vote(db_session, "explicit", False, "Voted incorrect, then removed")
        db_session.get(ProcessedEmail, "explicit").error_message = repository.REMOVED_BY_USER_MESSAGE
        db_session.commit()

        assert repository.get_correction_rate(db_session) == pytest.approx(1 / 3)  # e1 correct; e2 + explicit incorrect
        weeks = metrics.weekly_correction_rate(db_session)
        assert (weeks[-1]["correct"], weeks[-1]["total"]) == (1, 3)

    def test_a_plain_reschedule_is_not_a_vote(self, db_session):
        """The user may want a different time without the extraction being wrong."""
        _completed_row(db_session, "e1")
        repository.reschedule_email(db_session, "e1", datetime.now(timezone.utc) + timedelta(days=9), has_time=True)

        assert db_session.get(ProcessedEmail, "e1").user_correction is None
        assert repository.get_correction_rate(db_session) is None  # no votes at all


# --- Incorrect and Reschedule on a row with a live event --------------------------------------
@pytest.fixture
def event_calls(monkeypatch):
    """Stub the Calendar: record updates and deletions to the event."""
    calls = {"updated": [], "deleted": []}
    monkeypatch.setattr(calendar_client, "get_calendar_service", lambda: object())
    monkeypatch.setattr(calendar_client, "update_event", lambda service, **kw: calls["updated"].append(kw))
    monkeypatch.setattr(calendar_client, "delete_event", lambda service, event_id: calls["deleted"].append(event_id))
    monkeypatch.setattr(date_utils, "CALENDAR_TIMEZONE", "America/Los_Angeles")
    return calls


class TestIncorrectAndRescheduleOnALiveEvent:
    def test_an_unvoted_live_row_offers_correct_incorrect_reschedule_and_remove(self, client, db_session):
        _completed_row(db_session)

        page = client.get("/").text

        assert '"is_correct": "true"' in page and "Correct" in page
        assert '"is_correct": "false"' in page and "Incorrect" in page
        assert "/emails/e1/reschedule" in page
        assert "/emails/e1/remove" in page

    def test_incorrect_records_the_vote_without_touching_the_calendar_event(self, client, db_session, event_calls):
        _completed_row(db_session)

        response = client.post("/emails/e1/correct", data={"is_correct": "false"})

        assert response.status_code == 200
        row = db_session.get(ProcessedEmail, "e1")
        assert row.user_correction is False
        assert row.calendar_event_id == "cal-1"  # the event is still there
        assert event_calls == {"updated": [], "deleted": []}  # and was not modified

    def test_after_incorrect_the_row_offers_reschedule_and_remove_but_no_more_voting(
        self, client, db_session, event_calls
    ):
        _completed_row(db_session)

        response = client.post("/emails/e1/correct", data={"is_correct": "false"})

        assert response.headers["HX-Refresh"] == "true"  # the row moves section, so the page reloads
        html = _section(client.get("/").text, "marked_incorrect")
        assert "Marked incorrect — choose reschedule or remove" in html  # tells the user what to do next
        assert "/emails/e1/reschedule" in html and "Reschedule" in html
        assert "/emails/e1/remove" in html
        assert '"is_correct"' not in html  # the voting buttons are gone once a verdict exists

    def test_reschedule_alone_moves_the_event_and_records_no_verdict(self, client, db_session, event_calls):
        _completed_row(db_session)

        response = client.post("/emails/e1/reschedule", data={"new_datetime": "2026-10-05T14:30"})

        assert response.status_code == 200
        assert len(event_calls["updated"]) == 1  # the real event was moved
        assert db_session.get(ProcessedEmail, "e1").user_correction is None
        assert repository.get_correction_rate(db_session) is None

    def test_incorrect_then_reschedule_keeps_the_wrong_vote_and_fixes_the_time(
        self, client, db_session, event_calls
    ):
        _completed_row(db_session)
        client.post("/emails/e1/correct", data={"is_correct": "false"})

        client.post("/emails/e1/reschedule", data={"new_datetime": "2026-10-05T14:30"})

        row = db_session.get(ProcessedEmail, "e1")
        assert row.user_correction is False  # still marked incorrect
        assert row.extraction_deadline_parsed == datetime(2026, 10, 5, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert len(event_calls["updated"]) == 1
        assert repository.get_correction_rate(db_session) == 0.0

    def test_a_correct_verdict_hides_the_row_but_keeps_the_vote_and_the_event(self, client, db_session, event_calls):
        """The user does not want to keep seeing something once it's confirmed right — but the
        vote still counts for the correction-rate stat, and the real Calendar event is untouched
        (only Remove deletes it; marking correct never does)."""
        row = _completed_row(db_session)

        response = client.post("/emails/e1/correct", data={"is_correct": "true"})

        assert response.headers["HX-Refresh"] == "true"
        page = client.get("/").text
        assert "Nominations due" not in page  # the default _completed_row subject
        assert 'id="row-e1"' not in page
        db_session.refresh(row)
        assert row.user_correction is True and row.calendar_event_id == "cal-1"
        assert repository.get_correction_rate(db_session) == 1.0

    def test_incorrect_then_remove_keeps_the_explicit_vote_in_the_statistics(self, client, db_session, event_calls):
        _completed_row(db_session, "e1")
        _completed_row(db_session, "e2", event_id="cal-2", subject="Kept")
        repository.set_correction(db_session, "e2", True)
        client.post("/emails/e1/correct", data={"is_correct": "false"})

        client.post("/emails/e1/remove")

        assert repository.list_recent_emails(db_session)[0].email_id == "e2"  # e1 is hidden
        assert event_calls["deleted"] == ["cal-1"]
        assert repository.get_correction_rate(db_session) == 0.5  # the explicit incorrect vote still counts


class TestParkedEmailBanner:
    """An email that failed for good must be visible on the main page, not only in the history list."""

    def _fail(self, db_session, email_id, subject="Some subject", error="ServerError: 503 UNAVAILABLE", attempts=3):
        repository.try_claim_email(db_session, email_id, f"t-{email_id}", subject)
        repository.mark_failed(db_session, email_id, error)
        db_session.get(ProcessedEmail, email_id).attempt_count = attempts
        db_session.commit()

    def test_a_parked_email_is_called_out_with_its_subject_and_error(self, client, db_session):
        self._fail(db_session, "e1", subject="Direct Consideration #007")

        html = client.get("/").text

        assert "1 email failed and will not be retried automatically." in html
        assert "Direct Consideration #007" in html
        assert "503 UNAVAILABLE" in html

    def test_it_says_what_to_do_about_a_temporary_error(self, client, db_session):
        self._fail(db_session, "e1")

        assert "python -m scripts.retry_failed --apply" in client.get("/").text

    def test_several_are_pluralised_and_the_extras_counted(self, client, db_session):
        for i in range(7):
            self._fail(db_session, f"e{i}", subject=f"Subject number {i}")

        html = client.get("/").text

        assert "7 emails failed and will not be retried automatically." in html
        assert "and 2 more" in html  # five are listed

    def test_no_banner_when_nothing_has_failed_for_good(self, client, db_session):
        assert "will not be retried automatically" not in client.get("/").text

    def test_an_email_that_will_still_be_retried_does_not_trigger_it(self, client, db_session):
        self._fail(db_session, "e1", attempts=1)

        assert "will not be retried automatically" not in client.get("/").text

    def test_a_subject_containing_html_is_escaped(self, client, db_session):
        self._fail(db_session, "e1", subject="<script>alert(1)</script>", error="<img src=x onerror=alert(2)>")

        html = client.get("/").text

        assert "<script>alert(1)</script>" not in html and "<img src=x onerror=alert(2)>" not in html
        assert "&lt;script&gt;" in html

    def test_a_long_error_is_shortened(self, client, db_session):
        self._fail(db_session, "e1", error="ServerError: " + "x" * 500)

        html = client.get("/").text

        assert "x" * 200 not in html


class TestCategorySections:
    """The main page groups emails by the decision made about each (app/categories.py)."""

    def _fail(self, db, email_id, subject="A failed one", attempts=1):
        repository.try_claim_email(db, email_id, f"t-{email_id}", subject)
        repository.mark_failed(db, email_id, "ValueError: bad")
        db.get(ProcessedEmail, email_id).attempt_count = attempts
        db.commit()

    def _everything(self, db):
        _completed_row(db, "review", subject="Held back item", event_id=None)
        _completed_row(db, "check", subject="Auto added item", event_id="cal-1")
        _completed_row(db, "right", subject="Right item", event_id="cal-2")
        repository.set_correction(db, "right", True)
        _completed_row(db, "wrong", subject="Wrong item", event_id="cal-3")
        repository.set_correction(db, "wrong", False)
        _completed_row(db, "declined", subject="Declined item", event_id=None)
        repository.mark_skipped(db, "declined", "declined by user")
        self._fail(db, "broke", subject="Failed item")
        _completed_row(db, "todo", subject="Reply to advisor", event_id=None, action_type="needs_reply")

    def test_each_decision_lands_in_its_own_section_and_only_that_one(self, client, db_session):
        self._everything(db_session)
        page = client.get("/").text
        expected = {
            "needs_review": "Held back item", "to_check": "Auto added item",
            "marked_incorrect": "Wrong item", "skipped": "Declined item", "failed": "Failed item",
        }

        for key, subject in expected.items():
            assert subject in _section(page, key), f"{subject!r} should be under {key}"
            for other in expected:
                if other != key:
                    assert subject not in _section(page, other), f"{subject!r} leaked into {other}"
        # marked_correct is tracked (it still counts toward the correction rate) but never rendered
        assert 'id="section-marked_correct"' not in page
        assert "Right item" not in page

    def test_action_items_stay_in_their_own_panel_not_in_a_section(self, client, db_session):
        self._everything(db_session)
        page = client.get("/").text

        assert "Reply to advisor" in page
        assert not any("Reply to advisor" in _section(page, k) for k in ("needs_review", "to_check", "skipped", "failed"))

    def test_sections_appear_in_priority_order_with_their_counts(self, client, db_session):
        self._everything(db_session)
        _completed_row(db_session, "check2", subject="Second auto added", event_id="cal-9")

        page = client.get("/").text

        order = [page.index(f'id="section-{k}"') for k in ("needs_review", "to_check", "marked_incorrect", "failed", "skipped")]
        assert order == sorted(order)
        assert re.search(r"On your calendar</strong>\s*<span[^>]*>2</span>", page)  # a true count, not a guess

    def test_denied_or_skipped_renders_below_even_in_progress(self, client, db_session):
        self._everything(db_session)
        repository.try_claim_email(db_session, "busy", "t", "Being worked on")  # PROCESSING, stays claimed

        page = client.get("/").text

        assert page.index('id="section-in_progress"') < page.index('id="section-skipped"')

    def test_denied_or_skipped_renders_below_the_action_items_panel(self, client, db_session):
        self._everything(db_session)

        page = client.get("/").text

        assert page.index(">Action items<") < page.index('id="section-skipped"')
        assert 'id="section-skipped"' not in _section(page, "needs_review")  # still a real, complete section

    def test_denied_or_skipped_still_pages_and_links_back_to_itself(self, client, db_session):
        for i in range(30):
            _completed_row(db_session, f"d{i:02d}", subject=f"Declined {i:02d}", event_id=None)
            repository.mark_skipped(db_session, f"d{i:02d}", "declined by user")

        page = client.get("/").text

        assert _section(page, "skipped").count('id="row-d') == 25
        assert "Page 1 of 2" in _section(page, "skipped")
        second = client.get("/?p_skipped=2").text
        assert _section(second, "skipped").count('id="row-d') == 5

    def test_the_two_working_sections_always_show_and_say_so_when_empty(self, client):
        page = client.get("/").text

        assert 'id="section-needs_review"' in page and 'id="section-to_check"' in page
        assert "Nothing here." in _section(page, "needs_review")
        assert 'id="section-marked_correct"' not in page and 'id="section-failed"' not in page

    def test_sections_wanting_a_decision_start_open_and_finished_ones_start_closed(self, client, db_session):
        self._everything(db_session)
        page = client.get("/").text

        for key in ("needs_review", "to_check", "marked_incorrect", "failed"):
            assert re.search(rf'<details[^>]*id="section-{key}"', _section(page, key)) and " open" in _section(page, key).split(">")[0]
        assert " open" not in _section(page, "skipped").split(">")[0]

    def test_a_removed_email_appears_in_no_section(self, client, db_session, event_calls):
        _completed_row(db_session, "gone", subject="Removed item")
        client.post("/emails/gone/remove")

        assert "Removed item" not in client.get("/").text

    def test_each_section_pages_on_its_own_and_keeps_the_others_place(self, client, db_session):
        for i in range(30):
            _completed_row(db_session, f"c{i:02d}", subject=f"Check {i:02d}", event_id=f"cal-c{i}")
        for i in range(30):
            _completed_row(db_session, f"m{i:02d}", subject=f"Wrong {i:02d}", event_id=f"cal-m{i}")
            repository.set_correction(db_session, f"m{i:02d}", False)

        first = client.get("/").text
        second = client.get("/?p_to_check=2&p_marked_incorrect=2").text

        assert first.count('id="row-c') == 25 and first.count('id="row-m') == 25
        assert "Page 1 of 2" in _section(first, "to_check")
        assert _section(second, "to_check").count('id="row-c') == 5  # 30 - 25
        assert _section(second, "marked_incorrect").count('id="row-m') == 5
        # paging one section keeps where the other is
        assert "p_marked_incorrect=2" in _section(second, "to_check") or "p_to_check=1" in _section(second, "to_check")
        newer = re.search(r'href="([^"]*)#section-to_check">&larr; Newer', second).group(1)
        assert "p_marked_incorrect=2" in newer and "p_to_check=1" in newer

    @pytest.mark.parametrize("value,expected_page", [("99", 2), ("0", 1), ("-3", 1), ("abc", 1), ("", 1)])
    def test_a_bad_page_number_is_clamped_not_an_error(self, client, db_session, value, expected_page):
        for i in range(30):
            _completed_row(db_session, f"c{i:02d}", subject=f"Check {i:02d}", event_id=f"cal-c{i}")

        response = client.get(f"/?p_to_check={value}")

        assert response.status_code == 200
        assert f"Page {expected_page} of 2" in _section(response.text, "to_check")

    def test_a_subject_containing_html_is_escaped_inside_a_section(self, client, db_session):
        _completed_row(db_session, "x1", subject="<script>alert(1)</script>", event_id=None)

        html = _section(client.get("/").text, "needs_review")

        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


class TestDecisionsMoveAnEmailBetweenSections:
    """A button press changes the decision, so the email must appear in its new section after the reload."""

    def test_approving_moves_it_from_needs_review_to_to_check(self, client, db_session, calendar):
        _completed_row(db_session, "r1", subject="Workshop signup", event_id=None)
        assert "Workshop signup" in _section(client.get("/").text, "needs_review")

        response = client.post("/emails/r1/approve")

        page = client.get("/").text
        assert response.headers["HX-Refresh"] == "true"
        assert "Workshop signup" in _section(page, "to_check") and "Workshop signup" not in _section(page, "needs_review")

    def test_declining_moves_it_to_skipped(self, client, db_session):
        _completed_row(db_session, "r1", subject="Workshop signup", event_id=None)

        client.post("/emails/r1/decline")

        page = client.get("/").text
        assert "Workshop signup" in _section(page, "skipped") and "Workshop signup" not in _section(page, "needs_review")

    def test_voting_correct_removes_it_from_view_but_keeps_it_correct_in_the_stats(self, client, db_session):
        _completed_row(db_session, "c1", subject="Rent due")

        client.post("/emails/c1/correct", data={"is_correct": "true"})

        page = client.get("/").text
        assert "Rent due" not in page and "Rent due" not in _section(page, "to_check")
        assert repository.get_correction_rate(db_session) == 1.0

    def test_every_action_asks_the_browser_to_reload(self, client, db_session, calendar, monkeypatch):
        monkeypatch.setattr(calendar_client, "update_event", lambda service, **kw: None)
        _completed_row(db_session, "r1", event_id=None)
        _completed_row(db_session, "c1", subject="Rent due", event_id="cal-1")

        responses = [
            client.post("/emails/r1/approve"),
            client.post("/emails/c1/correct", data={"is_correct": "true"}),
            client.post("/emails/c1/reschedule", data={"new_datetime": "2026-10-05T14:30"}),
            client.post("/emails/c1/remove"),
        ]

        assert all(r.status_code == 200 and r.headers["HX-Refresh"] == "true" for r in responses)

    def test_buttons_no_longer_try_to_swap_a_single_row(self, client, db_session):
        _completed_row(db_session, "r1", event_id=None)
        _completed_row(db_session, "c1", subject="Rent due", event_id="cal-1")

        page = client.get("/").text

        assert 'hx-target="#row-' not in page
        assert 'hx-swap="none"' in page


class TestNeedsReviewOffersApproveRescheduleAndDismiss:
    def _held_back_without_a_date(self, db, email_id="nodate", subject="Vague deadline"):
        repository.try_claim_email(db, email_id, f"t-{email_id}", subject)
        repository.mark_completed(
            db, email_id,
            ExtractionResult(email_id=email_id, event_name=subject, deadline_date_raw=None, deadline_date=None,
                             source_context="ctx", confidence="low", action_type="deadline"),
            calendar_event_id=None,
        )

    def test_a_held_back_item_offers_approve_reschedule_and_deny(self, client, db_session):
        _completed_row(db_session, "r1", subject="Workshop signup", event_id=None)

        html = _section(client.get("/").text, "needs_review")

        assert "/emails/r1/approve" in html and "Approve" in html
        assert "/emails/r1/approve-at" in html and "Reschedule" in html and 'type="datetime-local"' in html
        assert "/emails/r1/decline" in html and "Dismiss" in html
        assert "Don't add" not in html and "Don&#39;t add" not in html

    def test_an_item_with_no_date_cannot_be_approved_so_it_is_not_offered_but_can_be_rescheduled(self, client, db_session):
        self._held_back_without_a_date(db_session)

        html = _section(client.get("/").text, "needs_review")

        assert "/emails/nodate/approve\"" not in html  # approve would only ever fail
        assert "/emails/nodate/approve-at" in html and "/emails/nodate/decline" in html

    def test_rescheduling_adds_it_at_the_chosen_local_time_and_moves_it_to_to_check(self, client, db_session, calendar, monkeypatch):
        monkeypatch.setattr(date_utils, "CALENDAR_TIMEZONE", "America/Los_Angeles")
        _completed_row(db_session, "r1", subject="Workshop signup", event_id=None)

        response = client.post("/emails/r1/approve-at", data={"new_datetime": "2026-10-05T14:30"})

        page = client.get("/").text
        assert response.status_code == 200 and response.headers["HX-Refresh"] == "true"
        assert calendar["created"][0]["deadline"] == datetime(2026, 10, 5, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        assert "Workshop signup" in _section(page, "to_check") and "Workshop signup" not in _section(page, "needs_review")

    def test_an_item_with_no_date_can_be_added_by_rescheduling_it(self, client, db_session, calendar, monkeypatch):
        monkeypatch.setattr(date_utils, "CALENDAR_TIMEZONE", "America/Los_Angeles")
        self._held_back_without_a_date(db_session)

        client.post("/emails/nodate/approve-at", data={"new_datetime": "2026-10-05T09:00"})

        assert "Vague deadline" in _section(client.get("/").text, "to_check")

    def test_an_invalid_time_is_refused_and_creates_nothing(self, client, db_session, calendar):
        _completed_row(db_session, "r1", event_id=None)

        assert client.post("/emails/r1/approve-at", data={"new_datetime": "not a time"}).status_code == 400
        assert client.post("/emails/r1/approve-at", data={}).status_code == 422
        assert calendar["created"] == []

    def test_rescheduling_something_already_on_the_calendar_or_unknown_is_refused(self, client, db_session, calendar):
        _completed_row(db_session, "c1", event_id="cal-1")

        assert client.post("/emails/c1/approve-at", data={"new_datetime": "2026-10-05T14:30"}).status_code == 400
        assert client.post("/emails/nope/approve-at", data={"new_datetime": "2026-10-05T14:30"}).status_code == 404
        assert calendar["created"] == []

    def test_deny_moves_it_to_the_denied_or_skipped_section(self, client, db_session):
        _completed_row(db_session, "r1", subject="Workshop signup", event_id=None)

        client.post("/emails/r1/decline")

        page = client.get("/").text
        assert "Denied or skipped" in page
        assert "Workshop signup" in _section(page, "skipped") and "Workshop signup" not in _section(page, "needs_review")


class TestActionItemDismiss:
    """Alongside Schedule, an action item can be denied — the item leaves the list, and nothing
    is ever created on the calendar for it (it isn't 'wrong', there's just nothing to do)."""

    def test_denying_removes_it_from_the_action_items_list_and_creates_no_event(self, client, db_session, create_calls):
        _action_item(db_session)

        response = client.post("/emails/a1/decline")

        assert response.status_code == 200 and response.headers["HX-Refresh"] == "true"
        assert repository.list_action_items(db_session) == []
        assert repository.count_action_items(db_session) == 0
        assert create_calls["created"] == []

    def test_a_denied_action_item_does_not_reappear_anywhere_on_the_page(self, client, db_session, create_calls):
        _action_item(db_session, subject="Reply to advisor about thesis")
        client.post("/emails/a1/decline")

        assert "Reply to advisor about thesis" not in client.get("/").text

    def test_deny_is_offered_next_to_schedule(self, client, db_session):
        _action_item(db_session)

        html = client.get("/").text

        assert "/emails/a1/schedule" in html
        assert "/emails/a1/decline" in html and "Dismiss" in html

    def test_denying_an_unknown_action_item_is_a_404(self, client):
        assert client.post("/emails/nope/decline").status_code == 404

    def test_the_underlying_row_is_kept_not_deleted(self, client, db_session):
        _action_item(db_session)

        client.post("/emails/a1/decline")

        row = db_session.get(ProcessedEmail, "a1")
        assert row is not None and row.status == ProcessingStatus.SKIPPED


class TestActionItemsRangeButtons:
    """The user picks the time window for Action Items explicitly (24h / 4d / 7d / 30d / older
    than 4 days / all time), each a button; the app no longer guesses a fixed cutoff."""

    @pytest.fixture(autouse=True)
    def _pin_timezone(self, monkeypatch):
        from app import date_utils

        monkeypatch.setattr(date_utils, "CALENDAR_TIMEZONE", "America/Los_Angeles")

    def _age(self, db_session, email_id, days):
        row = db_session.get(ProcessedEmail, email_id)
        row.updated_at = datetime.now(timezone.utc) - timedelta(days=days)
        db_session.commit()

    def test_all_six_buttons_are_offered(self, client):
        html = client.get("/").text

        for label in ("Last 24 hours", "Last 4 days", "Last 7 days", "Last 30 days", "Older than 4 days", "All time"):
            assert label in html

    def test_all_time_is_the_default_and_shows_everything(self, client, db_session):
        _action_item(db_session, "a1", subject="Recent one")
        _action_item(db_session, "a2", subject="Ancient one")
        self._age(db_session, "a2", days=400)

        html = client.get("/").text

        assert "Recent one" in html and "Ancient one" in html
        assert '>All time</span>' in html  # rendered as the active (non-link) button

    def test_last_24_hours_hides_anything_older(self, client, db_session):
        _action_item(db_session, "a1", subject="Today")
        _action_item(db_session, "a2", subject="Yesterday-ish")
        self._age(db_session, "a2", days=2)

        html = client.get("/?action_range=24h").text

        assert "Today" in html and "Yesterday-ish" not in html

    def test_older_than_4_days_hides_anything_recent(self, client, db_session):
        _action_item(db_session, "a1", subject="Recent")
        _action_item(db_session, "a2", subject="Old")
        self._age(db_session, "a2", days=10)

        html = client.get("/?action_range=older_4d").text

        assert "Old" in html and "Recent" not in html

    def test_an_item_comfortably_inside_four_days_is_included_in_last_4_days(self, client, db_session):
        _action_item(db_session, "a1", subject="Inside the window")
        row = db_session.get(ProcessedEmail, "a1")
        row.updated_at = datetime.now(timezone.utc) - timedelta(days=3, hours=23)  # just under 4 days
        db_session.commit()

        html = client.get("/?action_range=4d").text

        assert "Inside the window" in html

    def test_an_unknown_range_value_falls_back_to_all_time_not_an_error(self, client, db_session):
        _action_item(db_session, "a1", subject="Something")

        response = client.get("/?action_range=nonsense")

        assert response.status_code == 200
        assert "Something" in response.text

    def test_nothing_in_range_says_so_plainly(self, client, db_session):
        _action_item(db_session, "a1", subject="Old")
        self._age(db_session, "a1", days=10)

        html = client.get("/?action_range=24h").text

        assert "Nothing here for this range." in html

    def test_the_active_button_is_not_a_link_the_others_are(self, client):
        html = client.get("/?action_range=7d").text

        assert '>Last 7 days</span>' in html  # active: not clickable
        assert 'href="/?action_range=4d' in html  # inactive: still a link

    def test_every_range_buttons_link_resets_to_page_one_however_which_page_you_are_on(self, client, db_session):
        for i in range(30):
            _action_item(db_session, f"a{i:02d}", subject=f"Item {i:02d}")

        page_two = client.get("/?action_page=2").text

        hrefs = re.findall(r'href="([^"]*action_range=[^"]*)"', page_two)
        assert hrefs and all("action_page=1" in h for h in hrefs)


class TestWhatAndWhenColumns:
    """The row splits into a Subject, a What (the extracted name) and a When (date, and time only
    if one was actually found — a date-only deadline must not show a fabricated time)."""

    def test_what_shows_the_extracted_name_separately_from_the_raw_subject(self, client, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "RE: fwd: URGENT!! please read")
        repository.mark_completed(
            db_session, "e1",
            ExtractionResult(email_id="e1", event_name="Rent due", deadline_date_raw="Sep 24",
                             deadline_date=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
                             source_context="c", confidence="high", action_type="deadline"),
            calendar_event_id="cal-1",
        )

        html = _section(client.get("/").text, "to_check")

        assert "RE: fwd: URGENT!! please read" in html and "Rent due" in html

    def test_a_date_only_deadline_does_not_fabricate_a_time(self, client, db_session):
        """The bug: a date-only string parses with the CURRENT wall-clock time as filler (see
        date_utils.parse_deadline_date), so the dashboard was showing e.g. '11:41 PM' for
        something that never had a time at all."""
        repository.try_claim_email(db_session, "e1", "t1", "Some deadline")
        repository.mark_completed(
            db_session, "e1",
            ExtractionResult(email_id="e1", event_name="Some deadline", deadline_date_raw="Sep 23, 2026",
                             deadline_date=datetime(2026, 9, 23, 23, 41, tzinfo=timezone.utc),
                             source_context="c", confidence="high", action_type="deadline"),
            calendar_event_id="cal-1",
        )
        assert db_session.get(ProcessedEmail, "e1").extraction_has_time is False  # sanity on the fixture

        html = _section(client.get("/").text, "to_check")

        assert "Sep 23, 2026" in html
        # Scoped to right after the date, not the whole row: the Processed column legitimately
        # always has a real time, and would otherwise make this assertion pass for the wrong reason.
        after_date = html.split("Sep 23, 2026", 1)[1][:15]
        assert "PM" not in after_date and "AM" not in after_date

    def test_a_timed_deadline_still_shows_its_time(self, client, db_session):
        repository.try_claim_email(db_session, "e1", "t1", "Timed thing")
        repository.mark_completed(
            db_session, "e1",
            ExtractionResult(email_id="e1", event_name="Timed thing", deadline_date_raw="Sep 23, 2026 at 5:00 PM",
                             deadline_date=datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
                             source_context="c", confidence="high", action_type="deadline"),
            calendar_event_id="cal-1",
        )
        assert db_session.get(ProcessedEmail, "e1").extraction_has_time is True

        html = _section(client.get("/").text, "to_check")

        assert "PM" in html or "AM" in html

    def test_no_date_at_all_says_so_plainly(self, client, db_session):
        row = _completed_row(db_session, "a1", subject="Reply to advisor", event_id=None, action_type="needs_reply")
        row.calendar_event_id = "cal-1"  # force it into the deadline table for this check, bypassing Schedule
        db_session.commit()

        html = client.get("/").text

        assert "no date" in html


class TestTrashEmailButton:
    """Trash is offered next to every row, independent of what section it's in, and independent
    of whether it has a Calendar event, a vote, or is an action item."""

    def test_offered_on_a_to_check_row(self, client, db_session):
        _completed_row(db_session, "e1", event_id="cal-1")

        html = _section(client.get("/").text, "to_check")

        assert "/emails/e1/trash" in html and "Trash email" in html

    def test_offered_on_a_needs_review_row(self, client, db_session):
        _completed_row(db_session, "r1", event_id=None)

        html = _section(client.get("/").text, "needs_review")

        assert "/emails/r1/trash" in html

    def test_offered_on_a_failed_row(self, client, db_session):
        repository.try_claim_email(db_session, "f1", "t", "s")
        repository.mark_failed(db_session, "f1", "ValueError: bad")

        html = _section(client.get("/").text, "failed")

        assert "/emails/f1/trash" in html

    def test_offered_on_an_action_item(self, client, db_session):
        _action_item(db_session, "a1")

        html = client.get("/").text

        assert "/emails/a1/trash" in html

    def test_trashing_moves_the_gmail_message_and_hides_the_row(self, client, db_session, calendar_calls, gmail):
        _completed_row(db_session, "e1", subject="Nominations due", event_id="cal-1")

        response = client.post("/emails/e1/trash")

        assert response.status_code == 200 and response.headers["HX-Refresh"] == "true"
        assert gmail["trashed"] == ["e1"]
        assert "Nominations due" not in client.get("/").text

    def test_trashing_does_not_delete_the_calendar_event(self, client, db_session, calendar_calls, gmail):
        _completed_row(db_session, "e1", event_id="cal-1")

        client.post("/emails/e1/trash")

        assert calendar_calls["deleted"] == []

    def test_trashing_an_unknown_email_is_a_404(self, client, gmail):
        assert client.post("/emails/nope/trash").status_code == 404
        assert gmail["trashed"] == []
