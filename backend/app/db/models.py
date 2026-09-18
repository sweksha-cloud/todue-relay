"""Schema for tracking every email the pipeline has looked at.

One row per Gmail message, keyed by email_id, created the moment a run
claims it for processing. This is the source of both idempotency (a
completed/skipped email is never re-sent to the LLM) and the audit trail
(the full extraction result is stored, not just a seen/unseen bit).

`is_stale` and `superseded_by_email_id` are set on the OLD row by
repository.mark_superseded when a newer email is recognized as a changed
version of the same deadline (its Calendar event is updated in place and the
new row records duplicate_of_email_id / date_changed_from — see
claude/tradeoffs/deadline-changed-policy.md and
claude/tradeoffs/duplicate-deadline-detection.md).
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class ProcessingStatus(str, enum.Enum):
    PROCESSING = "processing"  # claimed, LLM call in flight
    COMPLETED = "completed"  # extraction done, calendar event created (or correctly skipped, see extraction_confidence/deadline)
    SKIPPED = "skipped"  # extraction done, no real deadline found — terminal, will not be retried
    FAILED = "failed"  # attempt errored (LLM timeout, malformed response, API error) — eligible for retry


class Confidence(str, enum.Enum):
    HIGH = "high"
    LOW = "low"


class ActionType(str, enum.Enum):
    DEADLINE = "deadline"  # real date/time — flows to calendar-event creation
    NEEDS_REPLY = "needs_reply"  # scheduling/interview-style request, no fixed date
    UNCLEAR = "unclear"  # genuinely ambiguous


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"

    email_id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String, index=True)
    email_subject: Mapped[str] = mapped_column(String)

    status: Mapped[ProcessingStatus] = mapped_column(Enum(ProcessingStatus, name="processing_status"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)

    # Set when a run claims this email; used to detect a crashed attempt
    # (claimed_at older than STALE_CLAIM_MINUTES with status still PROCESSING)
    # so a restart can safely retry it instead of skipping or double-claiming.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    extraction_event_name: Mapped[str | None] = mapped_column(String, nullable=True)
    extraction_deadline_raw: Mapped[str | None] = mapped_column(String, nullable=True)
    extraction_deadline_parsed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extraction_source_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_confidence: Mapped[Confidence | None] = mapped_column(Enum(Confidence, name="confidence_level"), nullable=True)
    extraction_action_type: Mapped[ActionType | None] = mapped_column(Enum(ActionType, name="action_type"), nullable=True)
    # Whether extraction_deadline_raw encoded an actual time (vs. date-only)
    # — decides timed vs. all-day Calendar event at approve/auto-create time.
    extraction_has_time: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # A single email describing a repeating obligation ("rent due the 1st
    # of every month") -> a true recurring Calendar event (RRULE), not a
    # one-off. See claude/post-prod/recurring-events.md (decided 2026-09-15).
    extraction_is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    extraction_recurrence_rule: Mapped[str | None] = mapped_column(String, nullable=True)
    # Failed the plausibility check (too far past/future to trust) —
    # distinct from plain low confidence, so the dashboard can show "this
    # date looks wrong" separately from "this is just uncertain." Never
    # causes auto-rejection, only this flag on an otherwise-normal
    # needs-review row. See claude/tradeoffs/stale-implausible-date-handling.md.
    is_implausible_date: Mapped[bool] = mapped_column(Boolean, default=False)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    calendar_event_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Step 6 correction endpoint / Step 7 correct-incorrect toggle.
    # None = not yet reviewed by the user; True/False = their verdict.
    user_correction: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Deadline-changed / duplicate detection (decided 2026-09-15, see
    # claude/tradeoffs/duplicate-deadline-detection.md and
    # claude/tradeoffs/deadline-changed-policy.md). Set on the OLD row once
    # a newer email is recognized as referring to the same deadline.
    is_stale: Mapped[bool] = mapped_column(default=False)
    superseded_by_email_id: Mapped[str | None] = mapped_column(
        ForeignKey("processed_emails.email_id"), nullable=True
    )
    # Set on the NEW row when it was recognized as a duplicate/update of an
    # already-tracked deadline (points at the OLD row).
    duplicate_of_email_id: Mapped[str | None] = mapped_column(
        ForeignKey("processed_emails.email_id"), nullable=True
    )
    # Set on the NEW row only when the matched deadline's date actually
    # changed — holds the prior date so the dashboard can show "was X, now
    # Y" rather than just "something changed."
    date_changed_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"  # the run itself errored out (not: individual emails failed)


class PipelineRun(Base):
    """One row per scheduled/manual pipeline invocation — what Step 6's
    'status of the last run' endpoint and Step 7's status banner read from.
    """

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, name="run_status"), default=RunStatus.RUNNING)

    emails_fetched: Mapped[int] = mapped_column(Integer, default=0)
    emails_processed: Mapped[int] = mapped_column(Integer, default=0)  # completed or skipped
    emails_failed: Mapped[int] = mapped_column(Integer, default=0)
    # Of emails_fetched: how many never even got claimed, and why. Added
    # for the observability layer (2026-09-18) — the pre-filter's "never
    # even claimed, cheapest possible skip" design means a filtered-out
    # email otherwise leaves zero trace anywhere, so filter-pass-rate
    # tracking and the run-to-run anomaly flag both need this recorded
    # directly; it can't be reconstructed after the fact from anything else.
    emails_filtered_out: Mapped[int] = mapped_column(Integer, default=0)
    # Already COMPLETED/SKIPPED from a prior run — fetched again (still
    # within FETCH_WINDOW_DAYS/unread) but not re-claimed. Tracked
    # separately from emails_filtered_out so "filter pass rate" isn't
    # distorted by a run that happens to re-see a lot of old, already-done
    # mail rather than genuinely rejecting new mail.
    emails_already_terminal: Mapped[int] = mapped_column(Integer, default=0)
    # Emails that passed the pre-filter but were NOT claimed this run because
    # the daily Gemini call budget was spent (pipeline.run_pipeline). Left
    # untouched for a later run, so they leave no other trace — this counter is
    # the only record that mail is waiting. Added 2026-09-18; on the live Neon
    # DB via ALTER TABLE (no migration tool), see claude/tradeoffs/daily-call-budget.md.
    emails_deferred: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class OAuthToken(Base):
    """Persisted Google OAuth token (Gmail + Calendar), keyed by a fixed
    name (currently just "google" — one account). Stored here rather than
    only as a local file so it survives on ephemeral compute (GitHub
    Actions runners, later Lambda) — the local file
    (GMAIL_TOKEN_PATH) is kept in sync too, purely as a local-dev
    convenience, but the database is the durable source of truth.
    """

    __tablename__ = "oauth_tokens"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    token_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
