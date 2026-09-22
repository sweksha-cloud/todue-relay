"""Step 6/7: the FastAPI app that both serves the API and renders the
dashboard directly as HTML (see docs/design-decisions.md, decision 14 for
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

from app import calendar_client, categories, metrics, pipeline, review_actions
from app.date_utils import detect_local_timezone, to_local
from app.db import repository
from app.db.models import ActionType, Base, ProcessedEmail, ProcessingStatus
from app.db.session import engine, get_db
from app.view_helpers import (
    ACTION_TYPE_BADGE_CLASS,
    ACTION_TYPE_LABEL,
    STATUS_BADGE_CLASS,
    display_status,
)

app = FastAPI(title="ToDue Relay")

SECTION_PAGE_SIZE = 25
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


def _url_with(request: Request, **params) -> str:
    """This page's URL with some query parameters changed, the rest kept (so paging one section does not
    reset another)."""
    url = request.url.include_query_params(**params)
    return f"{url.path}?{url.query}" if url.query else url.path


def _page_param(request: Request, name: str, pages: int) -> int:
    try:
        page = int(request.query_params.get(name, "1"))
    except ValueError:
        page = 1
    return min(max(page, 1), pages)


def _refresh() -> HTMLResponse:
    """The answer to a button press. The email may have moved to a different section, so ask the browser
    (htmx) to reload the page rather than swap one row in place. The reload keeps the query string, so the
    person stays on the pages they were looking at."""
    return HTMLResponse(content="", headers={"HX-Refresh": "true"})


def _build_section(db: Session, request: Request, category: "categories.Category") -> dict | None:
    """One category's rows, page and paging links — or None if it has nothing and is allowed to
    stay hidden when empty. Its own function so "skipped" can be built and placed separately from
    the rest (see the dashboard route)."""
    total = repository.count_category(db, category.key)
    if total == 0 and not category.show_when_empty:
        return None
    pages = max(1, -(-total // SECTION_PAGE_SIZE))  # ceil div
    param = f"p_{category.key}"
    page = _page_param(request, param, pages)
    return {
        "category": category,
        "rows": repository.list_category(db, category.key, limit=SECTION_PAGE_SIZE, offset=(page - 1) * SECTION_PAGE_SIZE),
        "total": total,
        "page": page,
        "pages": pages,
        "newer_url": _url_with(request, **{param: page - 1}) if page > 1 else None,
        "older_url": _url_with(request, **{param: page + 1}) if page < pages else None,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    action_page: int = Query(1, ge=1),
    db: Session = Depends(get_db),
):
    # The emails, grouped by the decision made about each (app/categories.py). Each section pages on its own
    # (?p_<key>=N); the two working sections always show, the others only when they have something.
    # "skipped" is built and rendered separately, after Action Items, on purpose: it's the least
    # useful section to see first, and the user asked for it to sit below Action Items specifically,
    # not just last among the others.
    sections = []
    skipped_section = None
    for category in categories.CATEGORIES:
        if not category.visible_in_dashboard:
            continue  # e.g. marked_correct: tracked for the stats, never shown once confirmed right
        built = _build_section(db, request, category)
        if built is None:
            continue
        if category.key == "skipped":
            skipped_section = built
        else:
            sections.append(built)

    action_range = request.query_params.get("action_range", ACTION_RANGE_DEFAULT)
    if action_range not in ACTION_RANGE_VALUES:
        action_range = ACTION_RANGE_DEFAULT
    range_since, range_until = _action_range_bounds(action_range)

    total_action_items = repository.count_action_items(db, since=range_since, until=range_until)
    total_action_pages = max(1, -(-total_action_items // ACTION_ITEMS_PAGE_SIZE))
    action_page = min(action_page, total_action_pages)
    action_offset = (action_page - 1) * ACTION_ITEMS_PAGE_SIZE

    action_items = repository.list_action_items(
        db, limit=ACTION_ITEMS_PAGE_SIZE, offset=action_offset, since=range_since, until=range_until
    )
    latest_run = repository.get_latest_run(db)
    parked_count = repository.count_parked_failures(db)
    parked = repository.list_parked_failures(db, limit=5)
    correction_rate = repository.get_correction_rate(db)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    caught_this_week = repository.count_deadlines_caught_since(db, since)

    action_items_by_day = _group_by_day(action_items)
    action_range_buttons = [
        {
            "value": value,
            "label": label,
            "active": value == action_range,
            # Changing the range changes what page 1 even means, so it resets paging.
            "url": _url_with(request, action_range=value, action_page=1),
        }
        for value, label in ACTION_RANGES
    ]
    llm_usage = metrics.daily_llm_usage(db)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "sections": sections,
            "skipped_section": skipped_section,
            "action_items_by_day": action_items_by_day,
            "action_range_buttons": action_range_buttons,
            "latest_run": latest_run,
            "parked_count": parked_count,
            "parked": parked,
            "correction_rate": correction_rate,
            "caught_this_week": caught_this_week,
            "llm_usage": llm_usage,
            "action_page": action_page,
            "total_action_pages": total_action_pages,
            "action_newer_url": _url_with(request, action_page=action_page - 1) if action_page > 1 else None,
            "action_older_url": _url_with(request, action_page=action_page + 1) if action_page < total_action_pages else None,
        },
    )


@app.post("/waiting", response_class=HTMLResponse)
def check_waiting(request: Request):
    """On-demand "how many emails are waiting" — the dashboard's button for
    the same read-only check as `python -m scripts.run_pipeline --dry-run`
    (docs/design-decisions.md, decision 13). ALWAYS a dry run, hardcoded: it reads
    Gmail and the database, makes no Gemini call, claims nothing, and writes
    nothing, so clicking it can never spend quota or change pipeline state.
    (tests/test_dashboard.py pins that dry_run=True is the only way it's called.)

    POST, not GET, so a link prefetcher or crawler can't trigger a Gmail read.
    Unauthenticated like the rest of the dashboard — fine while local-only; it
    must be protected before any public hosting (see docs/design-decisions.md, decision 22).
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


# (query value, button label). Order is the order the buttons render in. The user picks one
# explicitly instead of the page guessing a cutoff — replaces an earlier fixed "last 4 days,
# older folds into one section" design with direct control: pick a window, or "older than 4
# days" to see exactly what used to be tucked away, or "All time" to see everything at once.
ACTION_RANGES: list[tuple[str, str]] = [
    ("24h", "Last 24 hours"),
    ("4d", "Last 4 days"),
    ("7d", "Last 7 days"),
    ("30d", "Last 30 days"),
    ("older_4d", "Older than 4 days"),
    ("forever", "All time"),
]
ACTION_RANGE_VALUES = {value for value, _ in ACTION_RANGES}
ACTION_RANGE_DEFAULT = "forever"  # shows everything — the same total set the old design always showed


def _action_range_bounds(value: str) -> tuple[datetime | None, datetime | None]:
    """(since, until) for one ACTION_RANGES value, as bounds on updated_at. None means no bound
    on that side. "older_4d" is the one inverse case: no lower bound, only an upper one."""
    now = datetime.now(timezone.utc)
    days_by_value = {"24h": 1, "4d": 4, "7d": 7, "30d": 30}
    if value in days_by_value:
        return now - timedelta(days=days_by_value[value]), None
    if value == "older_4d":
        return None, now - timedelta(days=4)
    return None, None  # "forever"


@app.post("/emails/{email_id}/correct", response_class=HTMLResponse)
def correct_email(
    request: Request,
    email_id: str,
    is_correct: bool = Form(...),
    db: Session = Depends(get_db),
):
    """Plain correct/incorrect verdict. It records the vote and nothing else: it never
    touches the Calendar, even on a row with a live event. Reschedule and Remove are
    separate actions that change the event; Incorrect and Reschedule can be used together
    (the vote says the extraction was wrong, Reschedule fixes the time).
    """
    try:
        row = review_actions.record_vote(db, email_id, is_correct)
    except review_actions.ActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _refresh()


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
    return _refresh()


@app.post("/emails/{email_id}/schedule", response_class=HTMLResponse)
def schedule_action_item(
    email_id: str,
    new_datetime: str = Form(...),
    db: Session = Depends(get_db),
):
    """Action items have no date, so no Calendar event. This lets the user pick one: it
    creates the event and moves the item out of the action-items list into the regular
    list, where it behaves like any scheduled deadline (Reschedule / Remove). The response
    is empty with HX-Refresh so the page reloads and the item appears in its new place.
    """
    row = db.get(ProcessedEmail, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such email")
    is_action_item = row.extraction_action_type in (ActionType.NEEDS_REPLY, ActionType.UNCLEAR)
    if not is_action_item or row.status != ProcessingStatus.COMPLETED or row.calendar_event_id:
        raise HTTPException(status_code=400, detail="Not an unscheduled action item")

    try:
        naive = datetime.fromisoformat(new_datetime)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date/time")
    deadline = naive.replace(tzinfo=ZoneInfo(detect_local_timezone()))

    service = calendar_client.get_calendar_service()
    event_id = calendar_client.create_event(
        service,
        summary=row.extraction_event_name or row.email_subject,
        description=row.extraction_source_context or "",
        deadline=deadline,
        has_time=True,
    )
    repository.schedule_action_item(db, email_id, deadline, event_id)
    return HTMLResponse(content="", headers={"HX-Refresh": "true"})


@app.post("/emails/{email_id}/remove", response_class=HTMLResponse)
def remove_email(request: Request, email_id: str, db: Session = Depends(get_db)):
    """The "wrong, just get rid of it" path — deletes the real Calendar
    event, not just the local record of it. The row is kept in the database
    (recorded as an "incorrect" vote, and so it is never re-added) but no longer
    listed, so the response is empty: htmx swaps the row out of the table.
    """
    try:
        review_actions.remove_event(db, email_id)
    except review_actions.ActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _refresh()


@app.post("/emails/{email_id}/trash", response_class=HTMLResponse)
def trash_email(request: Request, email_id: str, db: Session = Depends(get_db)):
    """Move the source Gmail message to Trash (recoverable there for 30 days) and hide the row.
    Independent of Remove: a live Calendar event, if there is one, is left alone."""
    try:
        review_actions.trash_email(db, email_id)
    except review_actions.ActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _refresh()


@app.post("/emails/{email_id}/approve", response_class=HTMLResponse)
def approve_email(request: Request, email_id: str, db: Session = Depends(get_db)):
    """Low-confidence 'needs review' checkmark: create the Calendar event
    the pipeline held back on, per the confidence-routing decision
    (docs/design-decisions.md, decision 9: auto-create high, queue low for review).
    """
    try:
        row = review_actions.approve(db, email_id)
    except review_actions.ActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _refresh()


@app.post("/emails/{email_id}/approve-at", response_class=HTMLResponse)
def approve_email_at(email_id: str, new_datetime: str = Form(...), db: Session = Depends(get_db)):
    """"Reschedule" on an item needing review: add it to the calendar at a time the person picks, for when
    the time the pipeline extracted is wrong or missing."""
    try:
        naive = datetime.fromisoformat(new_datetime)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date/time")
    deadline = naive.replace(tzinfo=ZoneInfo(detect_local_timezone()))
    try:
        review_actions.approve_at(db, email_id, deadline)
    except review_actions.ActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _refresh()


@app.post("/emails/{email_id}/decline", response_class=HTMLResponse)
def decline_email(request: Request, email_id: str, db: Session = Depends(get_db)):
    """The 'don't add' side of the same checkmark — permanently skip
    without creating a Calendar event."""
    try:
        row = review_actions.decline(db, email_id)
    except review_actions.ActionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return _refresh()
