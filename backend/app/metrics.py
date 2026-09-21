"""Observability layer (2026-09-18): aggregation queries over data the
pipeline already writes to Postgres for its own operational reasons
(idempotency tracking, the run-status banner, the correction-rate stat) —
deliberately not a new logging/tracking system. Nothing here writes
anything; it only reads and aggregates.

Known precision gaps, accepted rather than solved with new schema:
- The weekly correction rate is bucketed by a row's `updated_at`, which
  Postgres bumps on ANY change to the row — not just the vote. Approving,
  rescheduling, removing, superseding (mark_superseded), or folding a
  follow-up into (fold_action_item) a row all move it, so a vote can slide
  into a later week. Approximate, fine as a trend; a dedicated
  `corrected_at` column would fix it but was judged not worth a schema
  change at this project's scale.
- The count only sees calls THIS pipeline made. GEMINI_DAILY_QUOTA (20,
  confirmed in AI Studio) is per project and model, so anything else using
  the same project (AI Studio playground, another script) spends it too and
  is invisible here — on 2026-09-18 Google showed 20/20 used while this
  count read 15. See claude/tradeoffs/gemini-quota-tracking.md.

LLM usage is NOT subject to the updated_at problem: it's summed from
pipeline_runs (see daily_llm_usage), whose started_at is written once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import (
    FILTER_ANOMALY_MIN_SAMPLE_SIZE,
    FILTER_PASS_RATE_ANOMALY_THRESHOLD,
    GEMINI_DAILY_QUOTA,
    LLM_USAGE_WARNING_THRESHOLD_PCT,
)
from app.db.models import ActionType, PipelineRun, ProcessedEmail, ProcessingStatus
from app.db.repository import explicit_votes_only

# Gemini's requests-per-day quota resets at midnight Pacific (per Google's rate-limit
# docs), regardless of where this runs — so "today" for quota purposes is a Pacific day.
QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")


def _filter_pass_rate(run: PipelineRun) -> float | None:
    """Of the emails this run actually offered to the pre-filter (fetched
    minus ones already known from a prior run, which the filter never even
    saw), what fraction passed. None when there was nothing to evaluate.
    """
    evaluated = run.emails_fetched - run.emails_already_terminal
    if evaluated <= 0:
        return None
    return (evaluated - run.emails_filtered_out) / evaluated


def _bucket_completions_by_run(runs: list[PipelineRun], rows: list[ProcessedEmail]) -> dict[int, list[ProcessedEmail]]:
    """Which run completed each row — approximated by whether the row's
    completed_at falls inside that run's [started_at, finished_at] window.
    No direct run_id column exists (see claude/tradeoffs/observability-metrics.md
    decision 1); reliable here because only one run ever executes at a
    time in this pipeline, so the windows never overlap.
    """
    buckets: dict[int, list[ProcessedEmail]] = {run.id: [] for run in runs}
    sorted_runs = sorted(runs, key=lambda r: r.started_at)
    now = datetime.now(timezone.utc)
    for row in rows:
        if row.completed_at is None:
            continue
        for run in sorted_runs:
            window_end = run.finished_at or now
            if run.started_at <= row.completed_at <= window_end:
                buckets[run.id].append(row)
                break
    return buckets


def _outcome_counts(rows: list[ProcessedEmail]) -> dict[str, int]:
    """Of a run's completed rows, how many became a live Calendar event
    ("deadlines_auto_created" — covers a fresh create, a duplicate linked
    to an existing event, and a reschedule alike, since all three end in a
    live event either way), how many are sitting in the review queue
    (a deadline extraction with no event yet), and how many surfaced as a
    new action item (excludes one folded into an earlier one via
    fold_action_item — that one never showed up as a second entry, so it
    didn't "surface").
    """
    deadlines_auto_created = review_queue_items = action_items_surfaced = 0
    for row in rows:
        if row.extraction_action_type == ActionType.DEADLINE:
            if row.calendar_event_id is not None:
                deadlines_auto_created += 1
            else:
                review_queue_items += 1
        elif row.extraction_action_type in (ActionType.NEEDS_REPLY, ActionType.UNCLEAR):
            if row.duplicate_of_email_id is None:
                action_items_surfaced += 1
    return {
        "deadlines_auto_created": deadlines_auto_created,
        "review_queue_items": review_queue_items,
        "action_items_surfaced": action_items_surfaced,
    }


def run_history(session: Session, limit: int = 20) -> list[dict]:
    """Most recent runs, each with its filter-pass-rate, whether that rate
    is anomalous versus the trailing average of the runs before it
    (FILTER_PASS_RATE_ANOMALY_THRESHOLD, FILTER_ANOMALY_MIN_SAMPLE_SIZE —
    app/config.py), and how its completed emails broke down (see
    _outcome_counts). Computed newest-first, since each run's "trailing
    average" needs the runs before it, then returned newest-first to
    match every other list on the dashboard.
    """
    stmt = select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(limit + FILTER_ANOMALY_MIN_SAMPLE_SIZE)
    runs = list(session.execute(stmt).scalars().all())
    runs.reverse()  # oldest first, so each run's trailing window is the ones already processed

    completed_rows: list[ProcessedEmail] = []
    if runs:
        earliest_start = min(run.started_at for run in runs)
        completed_stmt = select(ProcessedEmail).where(
            ProcessedEmail.status == ProcessingStatus.COMPLETED,
            ProcessedEmail.completed_at >= earliest_start,
        )
        completed_rows = list(session.execute(completed_stmt).scalars().all())
    outcomes_by_run = _bucket_completions_by_run(runs, completed_rows)

    trailing_rates: list[float] = []
    rows: list[dict] = []
    for run in runs:
        pass_rate = _filter_pass_rate(run)
        evaluated = run.emails_fetched - run.emails_already_terminal

        anomaly = False
        if (
            pass_rate is not None
            and evaluated >= FILTER_ANOMALY_MIN_SAMPLE_SIZE
            and len(trailing_rates) >= 3  # need at least a few runs before flagging deviation from "the average"
        ):
            trailing_avg = sum(trailing_rates) / len(trailing_rates)
            anomaly = abs(pass_rate - trailing_avg) > FILTER_PASS_RATE_ANOMALY_THRESHOLD

        row = {
            "run": run,
            "filter_pass_rate": pass_rate,
            "sent_to_llm": run.emails_processed + run.emails_failed,
            "anomaly": anomaly,
        }
        row.update(_outcome_counts(outcomes_by_run.get(run.id, [])))
        rows.append(row)

        if pass_rate is not None and evaluated >= FILTER_ANOMALY_MIN_SAMPLE_SIZE:
            trailing_rates.append(pass_rate)
            trailing_rates = trailing_rates[-7:]  # trailing 7 *eligible* runs, not just the last 7 overall

    rows.reverse()  # back to newest-first for display
    return rows[:limit]


def weekly_correction_rate(session: Session, weeks: int = 12) -> list[dict]:
    """Correct/total votes bucketed by week, approximated via updated_at.

    Approximate, not exact: updated_at is "when was this row last changed
    for any reason," not "when was it voted on" — approving, rescheduling,
    removing, superseding, or folding a follow-up into the row all bump it
    too (see the module docstring), so a vote can land in a later week than
    the one it was cast in. Fine for a trend line; don't read a single
    week's number as precise. Oldest week first, for a left-to-right trend
    line/table.
    """
    week_col = func.date_trunc("week", ProcessedEmail.updated_at)
    stmt = (
        select(
            week_col.label("week"),
            func.count().label("total"),
            func.sum(case((ProcessedEmail.user_correction.is_(True), 1), else_=0)).label("correct"),
        )
        .where(ProcessedEmail.user_correction.is_not(None), explicit_votes_only())
        .group_by(week_col)
        .order_by(week_col.desc())
        .limit(weeks)
    )
    rows = list(session.execute(stmt).all())
    rows.reverse()

    return [
        {
            "week": row.week.date(),
            "correct": int(row.correct or 0),
            "total": row.total,
            "rate": (row.correct or 0) / row.total if row.total else None,
        }
        for row in rows
    ]


def _llm_calls_since(session: Session, since: datetime) -> int:
    """LLM calls made by runs that started at/after `since`. Every email a
    run claims makes exactly one extract_deadline call, and a run records
    those as emails_processed + emails_failed (the same "sent to LLM" figure
    run_history shows). Counts a call the API rejected with a 429 too — it
    may not consume quota, so this is a slight upper bound, the safe
    direction for a warning. Not visible until the run finishes (counters
    are written at the end), and a run killed mid-flight leaves zeros.
    """
    stmt = select(func.coalesce(func.sum(PipelineRun.emails_processed + PipelineRun.emails_failed), 0)).where(
        PipelineRun.started_at >= since
    )
    return int(session.execute(stmt).scalar_one())


def daily_llm_usage(session: Session) -> dict:
    """LLM calls made today (a Pacific day, matching when Gemini's daily
    quota resets) against GEMINI_DAILY_QUOTA — the limit that actually
    bites. There is no monthly cap in Google's docs; the old monthly-quota
    bar could sit near 0% on the same day the daily limit was exhausted.
    Also reports this month's total, informational only (no quota).
    """
    now = datetime.now(QUOTA_RESET_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    calls_today = _llm_calls_since(session, day_start)
    calls_this_month = _llm_calls_since(session, month_start)
    pct = (calls_today / GEMINI_DAILY_QUOTA * 100) if GEMINI_DAILY_QUOTA else None

    return {
        "calls": calls_today,
        "quota": GEMINI_DAILY_QUOTA,
        "pct": pct,
        "warning": pct is not None and pct >= LLM_USAGE_WARNING_THRESHOLD_PCT,
        "resets_at": day_start + timedelta(days=1),
        "calls_this_month": calls_this_month,
        "month_start": month_start.date(),
    }
