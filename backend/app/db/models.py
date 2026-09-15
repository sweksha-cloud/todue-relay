"""Schema for tracking every email the pipeline has looked at.

One row per Gmail message, keyed by email_id, created the moment a run
claims it for processing. This is the source of both idempotency (a
completed/skipped email is never re-sent to the LLM) and the audit trail
(the full extraction result is stored, not just a seen/unseen bit).

`is_stale` and `superseded_by_email_id` are provisioned but unused: they
exist so that whichever "deadline changed" policy gets picked (see
claude/tradeoffs/) can be implemented without an schema migration — a plain
update-in-place policy just never sets them.
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

    # Unused until the deadline-changed policy (claude/tradeoffs/) is decided.
    is_stale: Mapped[bool] = mapped_column(default=False)
    superseded_by_email_id: Mapped[str | None] = mapped_column(
        ForeignKey("processed_emails.email_id"), nullable=True
    )

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
