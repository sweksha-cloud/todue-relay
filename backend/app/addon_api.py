"""JSON API for the Gmail add-on: a summary, and the review actions (vote, approve, decline, remove).

Kept thin on purpose. The add-on runs in Apps Script, where code is awkward to test and can only be
seen in Gmail, so everything that *decides* something (what counts as "needs review", what "upcoming"
means, how a deadline is worded) happens here, in tested Python. The add-on only fetches and lays out
what it is given. Every route requires the authenticated owner (app/addon_auth.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import review_actions
from app.addon_auth import require_owner
from app.date_utils import to_local
from app.db import repository
from app.db.models import ActionType, ProcessedEmail, ProcessingStatus, RunStatus
from app.db.session import get_db
from app.view_helpers import display_status

SUMMARY_LIMIT = 10
# An event whose deadline was earlier today still belongs in "upcoming" for a day.
UPCOMING_GRACE = timedelta(days=1)

router = APIRouter(prefix="/api/addon", dependencies=[Depends(require_owner)])


class EmailView(BaseModel):
    email_id: str
    subject: str
    event_name: str | None
    status: str  # added | needs review | skipped | processing | failed
    confidence: str | None
    action_type: str | None
    deadline_iso: str | None
    deadline_text: str | None
    on_calendar: bool
    is_implausible_date: bool
    vote: str | None  # "correct" | "incorrect" | None
    actions: list[str]  # what the person may do now: approve, decline, vote_correct, vote_incorrect, remove


class RunView(BaseModel):
    status: str
    started_text: str
    fetched: int
    processed: int
    failed: int


class Counts(BaseModel):
    needs_review: int
    upcoming: int
    action_items: int


class Summary(BaseModel):
    counts: Counts
    needs_review: list[EmailView]
    upcoming: list[EmailView]
    action_items: list[EmailView]
    latest_run: RunView | None


def deadline_text(deadline: datetime | None, has_time: bool | None) -> str | None:
    """A short, human deadline in the configured timezone: "Thu Sep 24" or "Thu Sep 24, 5:00 PM"."""
    if deadline is None:
        return None
    local = to_local(deadline)
    text = f"{local:%a %b} {local.day}"
    if has_time:
        hour = local.hour % 12 or 12
        text += f", {hour}:{local:%M %p}"
    return text


def available_actions(row: ProcessedEmail) -> list[str]:
    """Which review actions make sense for this item right now. Decided here, not in the add-on, so
    the rule is tested Python and the add-on only draws the buttons it is told about."""
    if row.calendar_event_id:
        actions = ["remove"]
        if row.user_correction is None:  # a verdict is given once, as on the dashboard
            actions = ["vote_correct", "vote_incorrect", *actions]
        return actions
    is_deadline = row.extraction_action_type in (None, ActionType.DEADLINE)
    if row.status == ProcessingStatus.COMPLETED and is_deadline:  # "needs review"
        return (["approve"] if row.extraction_deadline_parsed else []) + ["decline"]
    return []  # action items, skipped, failed and in-progress rows: nothing to do from the card yet


def email_view(row: ProcessedEmail) -> EmailView:
    deadline = row.extraction_deadline_parsed
    vote = None if row.user_correction is None else ("correct" if row.user_correction else "incorrect")
    return EmailView(
        email_id=row.email_id,
        subject=row.email_subject,
        event_name=row.extraction_event_name,
        status=display_status(row),
        confidence=row.extraction_confidence.value if row.extraction_confidence else None,
        action_type=row.extraction_action_type.value if row.extraction_action_type else None,
        deadline_iso=to_local(deadline).isoformat() if deadline else None,
        deadline_text=deadline_text(deadline, row.extraction_has_time),
        on_calendar=bool(row.calendar_event_id),
        is_implausible_date=row.is_implausible_date,
        vote=vote,
        actions=available_actions(row),
    )


def _run_view(run) -> RunView | None:
    if run is None:
        return None
    status = run.status.value if isinstance(run.status, RunStatus) else str(run.status)
    return RunView(
        status=status,
        started_text=f"{to_local(run.started_at):%a %b} {to_local(run.started_at).day}, "
        f"{to_local(run.started_at).hour % 12 or 12}:{to_local(run.started_at):%M %p}",
        fetched=run.emails_fetched,
        processed=run.emails_processed,
        failed=run.emails_failed,
    )


@router.get("/summary", response_model=Summary)
def summary(db: Session = Depends(get_db)) -> Summary:
    """What the add-on's home card shows: what needs a decision, what is coming up on the
    calendar, the action items, and how the last run went."""
    since = datetime.now(timezone.utc) - UPCOMING_GRACE
    return Summary(
        counts=Counts(
            needs_review=repository.count_needs_review(db),
            upcoming=repository.count_upcoming_on_calendar(db, since),
            action_items=repository.count_action_items(db),
        ),
        needs_review=[email_view(r) for r in repository.list_needs_review(db, limit=SUMMARY_LIMIT)],
        upcoming=[email_view(r) for r in repository.list_upcoming_on_calendar(db, since, limit=SUMMARY_LIMIT)],
        action_items=[email_view(r) for r in repository.list_action_items(db, limit=SUMMARY_LIMIT)],
        latest_run=_run_view(repository.get_latest_run(db)),
    )


class VoteBody(BaseModel):
    vote: Literal["correct", "incorrect"]


class ActionResult(BaseModel):
    ok: bool
    message: str  # what to tell the person, e.g. "Added to your calendar"
    email: EmailView


def _run(action, message: str, *args) -> ActionResult:
    try:
        row = action(*args)
    except review_actions.ActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from None
    return ActionResult(ok=True, message=message, email=email_view(row))


@router.post("/emails/{email_id}/vote", response_model=ActionResult)
def vote(email_id: str, body: VoteBody, db: Session = Depends(get_db)) -> ActionResult:
    """A correct / incorrect verdict. Records the vote only; it never touches the calendar."""
    is_correct = body.vote == "correct"
    return _run(review_actions.record_vote, f"Marked {body.vote}", db, email_id, is_correct)


@router.post("/emails/{email_id}/approve", response_model=ActionResult)
def approve(email_id: str, db: Session = Depends(get_db)) -> ActionResult:
    """Create the calendar event the pipeline held back on."""
    return _run(review_actions.approve, "Added to your calendar", db, email_id)


@router.post("/emails/{email_id}/decline", response_model=ActionResult)
def decline(email_id: str, db: Session = Depends(get_db)) -> ActionResult:
    """Decline to add a held-back item: skipped for good, no calendar event."""
    return _run(review_actions.decline, "Won't add this", db, email_id)


@router.post("/emails/{email_id}/remove", response_model=ActionResult)
def remove(email_id: str, db: Session = Depends(get_db)) -> ActionResult:
    """Delete the item's real calendar event. The row is kept (so it is never re-added) but unlisted."""
    return _run(review_actions.remove_event, "Removed from your calendar", db, email_id)
