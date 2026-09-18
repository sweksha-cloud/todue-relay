"""Step 6/7: the FastAPI app that both serves the API and renders the
dashboard directly as HTML (see claude/tradeoffs/frontend-choice.md for
why plain server pages + htmx instead of a separate React app).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import calendar_client, metrics, pipeline
from app.date_utils import detect_local_timezone, to_local
from app.db import repository
from app.db.models import Base, ProcessedEmail, ProcessingStatus
from app.db.session import engine, get_db
from app.view_helpers import (
    ACTION_TYPE_BADGE_CLASS,
    ACTION_TYPE_LABEL,
    STATUS_BADGE_CLASS,
    display_status,
)

app = FastAPI(title="ToDue Relay")

HISTORY_PAGE_SIZE = 25
ACTION_ITEMS_PAGE_SIZE = 25

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["display_status"] = display_status
templates.env.globals["status_badge_class"] = lambda s: STATUS_BADGE_CLASS.get(s, "")
templates.env.globals["action_type_label"] = lambda t: ACTION_TYPE_LABEL.get(t, t)
templates.env.globals["action_type_badge_class"] = lambda t: ACTION_TYPE_BADGE_CLASS.get(t, "")
templates.env.filters["to_local"] = to_local


@app.on_event("startup")
def ensure_schema() -> None:
    # Personal-scale project, no separate migration tool yet — safe no-op
    # if tables already exist.
    if engine is not None:
        Base.metadata.create_all(engine)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    page: int = Query(1, ge=1),
    action_page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    total_emails = repository.count_recent_emails(db)
    total_pages = max(1, -(-total_emails // HISTORY_PAGE_SIZE))  # ceil div
    page = min(page, total_pages)
    offset = (page - 1) * HISTORY_PAGE_SIZE

    total_action_items = repository.count_action_items(db)
    total_action_pages = max(1, -(-total_action_items // ACTION_ITEMS_PAGE_SIZE))
    action_page = min(action_page, total_action_pages)
    action_offset = (action_page - 1) * ACTION_ITEMS_PAGE_SIZE

    emails = repository.list_recent_emails(db, limit=HISTORY_PAGE_SIZE, offset=offset)
    action_items = repository.list_action_items(db, limit=ACTION_ITEMS_PAGE_SIZE, offset=action_offset)
    latest_run = repository.get_latest_run(db)
    correction_rate = repository.get_correction_rate(db)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    caught_this_week = repository.count_deadlines_caught_since(db, since)

    action_items_by_day = _group_by_day(action_items)
    llm_usage = metrics.daily_llm_usage(db)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "emails": emails,
            "action_items_by_day": action_items_by_day,
            "latest_run": latest_run,
            "correction_rate": correction_rate,
            "caught_this_week": caught_this_week,
            "llm_usage": llm_usage,
            "page": page,
            "total_pages": total_pages,
            "action_page": action_page,
            "total_action_pages": total_action_pages,
        },
    )


@app.post("/waiting", response_class=HTMLResponse)
def check_waiting(request: Request):
    """On-demand "how many emails are waiting" — the dashboard's button for
    the same read-only check as `python -m scripts.run_pipeline --dry-run`
    (claude/tradeoffs/dry-run-mode.md). ALWAYS a dry run, hardcoded: it reads
    Gmail and the database, makes no Gemini call, claims nothing, and writes
    nothing, so clicking it can never spend quota or change pipeline state.
    (tests/test_dashboard.py pins that dry_run=True is the only way it's called.)

    POST, not GET, so a link prefetcher or crawler can't trigger a Gmail read.
    Unauthenticated like the rest of the dashboard — fine while local-only; it
    must be protected before any public hosting (see claude/tradeoffs/security-review.md).
    A failure is rendered as a message, not raised: htmx doesn't swap 4xx/5xx.
    """
    try:
        result, error = pipeline.run_pipeline(dry_run=True), None
    except Exception as e:  # noqa: BLE001 - shown to the user, not swallowed
        result, error = None, f"{type(e).__name__}: {e}"
    return templates.TemplateResponse(
        request,
        "_waiting.html",
        {"result": result, "error": error, "checked_at": datetime.now(timezone.utc)},
    )


@app.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request, db: Session = Depends(get_db)):
    """Observability layer (2026-09-18): a thin read-only view over
    aggregates in app/metrics.py — nothing here writes anything.
    """
    return templates.TemplateResponse(
        request,
        "metrics.html",
        {
            "run_history": metrics.run_history(db),
            "weekly_correction_rate": metrics.weekly_correction_rate(db),
            "llm_usage": metrics.daily_llm_usage(db),
        },
    )


def _group_by_day(rows: list) -> list[tuple]:
    """[(day, [rows that day]), ...], newest day first. Rows already come
    in newest-first order from the query, so this just needs to preserve
    that grouping without re-sorting.

    Groups by updated_at, not created_at — list_action_items orders by the
    same field, so a folded-into action item (fold_action_item bumps
    updated_at when a follow-up email arrives) resurfaces under today
    instead of staying pinned under whatever day it was first seen.
    """
    groups: dict = {}
    order: list = []
    for row in rows:
        day = to_local(row.updated_at).date()
        if day not in groups:
            groups[day] = []
            order.append(day)
        groups[day].append(row)
    return [(day, groups[day]) for day in order]


@app.post("/emails/{email_id}/correct", response_class=HTMLResponse)
def correct_email(
    request: Request,
    email_id: str,
    is_correct: bool = Form(...),
    db: Session = Depends(get_db),
):
    """Plain correct/incorrect audit flag — for rows with no live Calendar
    event (skipped/failed/declined items). For a row that *does* have one,
    the frontend routes "wrong" through /reschedule or /remove instead, so
    marking it wrong always keeps the calendar in sync with the verdict.
    """
    row = db.get(ProcessedEmail, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such email")
    if row.calendar_event_id and not is_correct:
        # "Correct" needs no calendar action, so it's always fine here —
        # only "wrong" must go through /reschedule or /remove instead, so
        # a live event is never left stale after a wrong verdict.
        raise HTTPException(
            status_code=400,
            detail="This item has a live Calendar event — use /reschedule or /remove instead",
        )

    row = repository.set_correction(db, email_id, is_correct)
    return templates.TemplateResponse(request, "_row.html", {"email": row})


@app.post("/emails/{email_id}/reschedule", response_class=HTMLResponse)
def reschedule_email(
    request: Request,
    email_id: str,
    new_datetime: str = Form(...),
    db: Session = Depends(get_db),
):
    """The "wrong, but here's the right time" path — patches the existing
    Calendar event in place rather than just recording a flag.
    """
    row = db.get(ProcessedEmail, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such email")
    if not row.calendar_event_id:
        raise HTTPException(status_code=400, detail="No live Calendar event to reschedule")

    try:
        naive = datetime.fromisoformat(new_datetime)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date/time")
    new_deadline = naive.replace(tzinfo=ZoneInfo(detect_local_timezone()))

    service = calendar_client.get_calendar_service()
    calendar_client.update_event(
        service,
        event_id=row.calendar_event_id,
        summary=row.extraction_event_name or row.email_subject,
        description=row.extraction_source_context or "",
        deadline=new_deadline,
        has_time=True,
    )
    row = repository.reschedule_email(db, email_id, new_deadline, has_time=True)
    return templates.TemplateResponse(request, "_row.html", {"email": row})


@app.post("/emails/{email_id}/remove", response_class=HTMLResponse)
def remove_email(request: Request, email_id: str, db: Session = Depends(get_db)):
    """The "wrong, just get rid of it" path — deletes the real Calendar
    event, not just the local record of it.
    """
    row = db.get(ProcessedEmail, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such email")
    if not row.calendar_event_id:
        raise HTTPException(status_code=400, detail="No live Calendar event to remove")

    service = calendar_client.get_calendar_service()
    calendar_client.delete_event(service, row.calendar_event_id)
    row = repository.remove_calendar_event(db, email_id)
    return templates.TemplateResponse(request, "_row.html", {"email": row})


@app.post("/emails/{email_id}/approve", response_class=HTMLResponse)
def approve_email(request: Request, email_id: str, db: Session = Depends(get_db)):
    """Low-confidence 'needs review' checkmark: create the Calendar event
    the pipeline held back on, per the confidence-routing decision
    (claude/tradeoffs/ — auto-create high, queue low for review).
    """
    row = db.get(ProcessedEmail, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such email")
    if row.status != ProcessingStatus.COMPLETED or row.calendar_event_id or not row.extraction_deadline_parsed:
        raise HTTPException(status_code=400, detail="Not an approvable item")

    service = calendar_client.get_calendar_service()
    event_id = calendar_client.create_event(
        service,
        summary=row.extraction_event_name or row.email_subject,
        description=row.extraction_source_context or "",
        deadline=row.extraction_deadline_parsed,
        has_time=bool(row.extraction_has_time),
        recurrence_rule=row.extraction_recurrence_rule if row.extraction_is_recurring else None,
    )
    row = repository.set_calendar_event(db, email_id, event_id)
    return templates.TemplateResponse(request, "_row.html", {"email": row})


@app.post("/emails/{email_id}/decline", response_class=HTMLResponse)
def decline_email(request: Request, email_id: str, db: Session = Depends(get_db)):
    """The 'don't add' side of the same checkmark — permanently skip
    without creating a Calendar event."""
    try:
        repository.mark_skipped(db, email_id, "declined by user (low-confidence review)")
    except ValueError:
        raise HTTPException(status_code=404, detail="No such email")

    row = db.get(ProcessedEmail, email_id)
    return templates.TemplateResponse(request, "_row.html", {"email": row})
