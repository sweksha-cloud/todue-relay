"""Renders the real dashboard pages (Jinja templates included) against the test
Postgres. Only the DB dependency is swapped; the app and templates are real.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.db import repository
from app.db.models import ProcessedEmail, ProcessingStatus, RunStatus
from app.schemas import ExtractionResult
from app import main, metrics, pipeline
from app.db.session import get_db
from app.main import app


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)  # no `with`: startup events (schema creation) don't run
    app.dependency_overrides.clear()


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
    monkeypatch.setattr(main.calendar_client, "get_calendar_service", lambda: object())
    monkeypatch.setattr(main.calendar_client, "delete_event", lambda service, event_id: calls["deleted"].append(event_id))
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

    monkeypatch.setattr(main.calendar_client, "get_calendar_service", lambda: object())
    monkeypatch.setattr(main.calendar_client, "create_event", fake_create)
    monkeypatch.setattr(main.calendar_client, "delete_event", lambda service, event_id: calls["deleted"].append(event_id))
    monkeypatch.setattr(main, "detect_local_timezone", lambda: "America/Los_Angeles")
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

    def test_removed_rows_are_excluded_from_both_rates_including_legacy_ones(self, db_session):
        """Rows removed before this change were stored as an incorrect vote with the old
        "(marked incorrect)" text; they must stop counting too, without a data migration.
        """
        _vote(db_session, "e1", True, "Kept and correct")
        _vote(db_session, "e2", False, "Wrong extraction")
        for email_id, marker in (
            ("r_new", repository.REMOVED_BY_USER_MESSAGE),
            ("r_legacy", "removed from calendar by user (marked incorrect)"),
        ):
            _vote(db_session, email_id, False, f"Removed {email_id}")
            db_session.get(ProcessedEmail, email_id).error_message = marker
        db_session.commit()

        assert repository.get_correction_rate(db_session) == 0.5  # 1 correct of the 2 real votes
        weeks = metrics.weekly_correction_rate(db_session)
        assert (weeks[-1]["correct"], weeks[-1]["total"]) == (1, 2)

    def test_a_reschedule_still_counts_as_a_wrong_vote(self, db_session):
        """Moving the time means the extracted time was wrong, which is a real verdict."""
        _completed_row(db_session, "e1")
        repository.reschedule_email(db_session, "e1", datetime.now(timezone.utc) + timedelta(days=9), has_time=True)

        assert repository.get_correction_rate(db_session) == 0.0
