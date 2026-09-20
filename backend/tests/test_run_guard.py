"""Single-flight guard for pipeline runs (repository.try_start_run).

Real Postgres, like the claim-logic tests: the guard is a Postgres advisory lock
plus a RUNNING row with an expiry, so a mock could not prove it works. The race
test is the important one: many runs starting at the same instant must yield
exactly one winner.
"""

import threading

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from app.db import repository
from app.db.models import PipelineRun, RunStatus

TTL = 30


def _running_count(session) -> int:
    session.rollback()  # end any open transaction so we read the latest committed state
    return session.execute(
        select(func.count()).select_from(PipelineRun).where(PipelineRun.status == RunStatus.RUNNING)
    ).scalar_one()


def _backdate(session, run_id: int, minutes: int) -> None:
    session.execute(
        text("UPDATE pipeline_runs SET started_at = now() - make_interval(mins => :m) WHERE id = :id"),
        {"m": minutes, "id": run_id},
    )
    session.commit()


class TestTryStartRun:
    def test_starts_a_run_when_none_is_in_progress(self, db_session):
        run = repository.try_start_run(db_session, ttl_minutes=TTL)

        assert run is not None
        assert run.status == RunStatus.RUNNING
        assert _running_count(db_session) == 1

    def test_refuses_while_another_run_is_in_progress_and_writes_nothing(self, db_session):
        first = repository.try_start_run(db_session, ttl_minutes=TTL)
        assert first is not None

        second = repository.try_start_run(db_session, ttl_minutes=TTL)

        assert second is None
        assert _running_count(db_session) == 1  # no second row

    def test_allows_a_new_run_once_the_previous_one_finished(self, db_session):
        first = repository.try_start_run(db_session, ttl_minutes=TTL)
        repository.finish_run(
            db_session, first.id, status=RunStatus.SUCCESS, emails_fetched=0, emails_processed=0, emails_failed=0
        )

        assert repository.try_start_run(db_session, ttl_minutes=TTL) is not None

    def test_a_failed_run_does_not_block_the_next_one(self, db_session):
        first = repository.try_start_run(db_session, ttl_minutes=TTL)
        repository.finish_run(
            db_session, first.id, status=RunStatus.FAILURE, emails_fetched=0, emails_processed=0,
            emails_failed=0, error_message="boom",
        )

        assert repository.try_start_run(db_session, ttl_minutes=TTL) is not None

    def test_a_crashed_runs_leftover_row_expires(self, db_session):
        """A run killed without finishing (Lambda/Actions timeout, dead process)
        leaves a RUNNING row. Past the TTL it must stop blocking new runs.
        """
        crashed = repository.try_start_run(db_session, ttl_minutes=TTL)
        _backdate(db_session, crashed.id, TTL + 1)

        assert repository.try_start_run(db_session, ttl_minutes=TTL) is not None

    def test_a_row_just_inside_the_ttl_still_blocks(self, db_session):
        recent = repository.try_start_run(db_session, ttl_minutes=TTL)
        _backdate(db_session, recent.id, TTL - 1)

        assert repository.try_start_run(db_session, ttl_minutes=TTL) is None


class TestConcurrentStarts:
    def test_exactly_one_of_many_simultaneous_starts_wins(self, engine, db_session):
        """8 separate connections all try to start at the same instant. Without the
        advisory lock, several would see 'nothing running' and all insert.
        """
        make_session = sessionmaker(bind=engine, expire_on_commit=False)
        n = 8
        barrier = threading.Barrier(n)
        winners: list[int] = []
        errors: list[Exception] = []

        def attempt():
            session = make_session()
            try:
                barrier.wait()
                run = repository.try_start_run(session, ttl_minutes=TTL)
                if run is not None:
                    winners.append(run.id)
            except Exception as e:  # noqa: BLE001 - surfaced via the assertion below
                errors.append(e)
            finally:
                session.close()

        threads = [threading.Thread(target=attempt) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(winners) == 1
        assert _running_count(db_session) == 1
