"""Renders the real dashboard pages (Jinja templates included) against the test
Postgres. Only the DB dependency is swapped; the app and templates are real.
"""

import pytest
from fastapi.testclient import TestClient

from app.db import repository
from app.db.models import RunStatus
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
