"""Renders the real dashboard pages (Jinja templates included) against the test
Postgres. Only the DB dependency is swapped; the app and templates are real.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import repository
from app.db.models import ProcessedEmail, ProcessingStatus, RunStatus
from app.schemas import ExtractionResult
from app import main, pipeline
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

    def test_a_removal_still_counts_as_an_incorrect_vote(self, client, db_session, calendar_calls):
        _completed_row(db_session, "e1")
        _completed_row(db_session, "e2", event_id="cal-2", subject="Kept")
        repository.set_correction(db_session, "e2", True)

        client.post("/emails/e1/remove")

        assert repository.get_correction_rate(db_session) == 0.5  # 1 correct, 1 incorrect (the removal)

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
