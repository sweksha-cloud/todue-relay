"""The demo video's data: the real pipeline run over made-up emails, never a real inbox or calendar.

    python -m scripts.demo_video run    # real pre-filter + real Gemini + real routing -> demo DB + trace
    python -m scripts.demo_video run --keep --redo demo-06   # rerun only some emails, keep the rest
    python -m scripts.demo_video serve  # the real dashboard over the demo DB, on http://localhost:8002

Both need DEMO_DATABASE_URL, a Postgres database whose name contains "demo" (refused otherwise, so the
production database can't be pointed at by mistake). Gmail is replaced by the fake emails below and
Google Calendar by an in-memory stand-in, so nothing here can read real mail or write a real event.
Gemini is real: `run` spends one call per email that passes the pre-filter (5 of the 9 below).

`run` also writes demo-video/src/data/trace.json: what each email scored on the pre-filter, the JSON
Gemini actually returned, and where the pipeline routed it. The video's pipeline scene is drawn from it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEMO_URL = os.environ.get("DEMO_DATABASE_URL", "")
if "demo" not in urlparse(DEMO_URL).path:
    sys.exit("DEMO_DATABASE_URL must point at a database whose name contains 'demo'.")
os.environ["DATABASE_URL"] = DEMO_URL  # before any app import: app.db.session reads it at import time

from app import alerts, calendar_client, gmail_client, llm_client, pipeline  # noqa: E402
from app.db.models import Base, ProcessedEmail  # noqa: E402
from app.db.session import engine, get_session  # noqa: E402
from app.filters import is_actionable_candidate, score_email  # noqa: E402
from app.gmail_client import EmailMessage  # noqa: E402
from app.view_helpers import display_status  # noqa: E402

TRACE_PATH = Path(__file__).resolve().parents[2] / "demo-video" / "src" / "data" / "trace.json"
RECEIVED = "Wed, 7 Oct 2026 09:12:00 -0700"


def _email(i: int, sender: str, subject: str, body: str) -> EmailMessage:
    return EmailMessage(id=f"demo-{i:02d}", thread_id=f"demo-thread-{i:02d}", subject=subject, sender=sender,
                        date=RECEIVED, snippet=body[:120], body_text=body)


# Shown in the inbox top to bottom; the first one is the email that "arrives" during the video.
FAKE_EMAILS = [
    _email(1, "Financial Aid Office <aid@northfield.edu>", "Reminder: scholarship application due Oct 20",
           "Hi Sam,\n\nThis is a reminder that the Merit Scholarship application is due Tuesday, October 20, "
           "2026 at 11:59 PM PT. Please submit your essay and transcript through the student portal.\n\n"
           "Financial Aid Office"),
    _email(2, "Design Club <hello@designclub.org>", "Product Design Info Session: Thursday, Oct 15",
           "Join us for a Product Design info session with alumni from three studios on Thursday, October 15 "
           "at 6:00 PM in Hall B. Seating is limited and registration closes on October 14.\n\nSee you there!"),
    _email(3, "Peakline Outdoors <deals@peakline.shop>", "New arrivals just landed",
           "Fresh jackets, trail shoes and packs for the season. Browse the new collection and find your "
           "next favorite gear. Free shipping for members."),
    _email(4, "Engineering Alumni <events@northfield.edu>", "RSVP: Fall Engineering Mixer",
           "You're invited to the Fall Engineering Mixer on October 22 at 7 PM. Please RSVP by Friday, "
           "October 16 so we can finalize catering. Hope to see you there."),
    _email(5, "Streamly <noreply@streamly.fm>", "Your weekly mix is ready",
           "We picked 30 songs we think you'll love, based on what you played this week. Open the app "
           "to listen."),
    _email(6, "Residential Life <housing@northfield.edu>", "Spring housing: renewal window",
           "Hi Sam,\n\nSpring housing renewals are tentatively due October 30, but that date may still "
           "move once room counts are final. We will confirm the deadline in a later email."),
    _email(7, "Brightside Coffee <rewards@brightside.cafe>", "You've earned a free drink",
           "Thanks for being a regular! Your next drink is on us. Show this email at any Brightside "
           "location."),
    _email(8, "Maya Chen <maya.chen@example.com>", "Coffee chat next week?",
           "Hey Sam, I'd love to hear about your internship search. Are you free for a coffee chat "
           "sometime next week? Let me know what works and I'll schedule it."),
    _email(9, "Jordan Lee <jordan.lee@example.com>", "Photos from the hike",
           "Uploaded the photos from Saturday's hike to the shared album. The one at the summit came out "
           "great."),
]


class FakeCalendar:
    """Stands in for Google Calendar: remembers events in memory, talks to nobody."""

    def __init__(self):
        self.events: dict[str, dict] = {}

    def create(self, service, *, summary, deadline, event_id=None, **kw) -> str:
        event_id = event_id or f"demo-event-{len(self.events) + 1}"
        self.events[event_id] = {"summary": summary, "deadline": deadline.isoformat(), **kw}
        return event_id

    def update(self, service, *, event_id, **kw) -> None:
        self.events.setdefault(event_id, {}).update({k: str(v) for k, v in kw.items()})

    def delete(self, service, event_id, calendar_id="primary") -> None:
        self.events.pop(event_id, None)


def _no_gmail(*a, **kw):
    raise RuntimeError("Gmail is not available in the demo")


def install_fakes() -> FakeCalendar:
    """Swap every outside service for a stand-in. Gemini (llm_client) is left real on purpose."""
    cal = FakeCalendar()
    calendar_client.get_calendar_service = lambda: object()
    calendar_client.create_event = cal.create
    calendar_client.update_event = cal.update
    calendar_client.delete_event = cal.delete
    gmail_client.get_gmail_service = _no_gmail
    gmail_client.trash_message = _no_gmail
    pipeline.get_gmail_service = lambda: object()
    pipeline.fetch_recent_messages = lambda service, **kw: list(FAKE_EMAILS)
    pipeline.fetch_messages_by_ids = lambda service, ids: [e for e in FAKE_EMAILS if e.id in ids]
    alerts.notify_parked = lambda *a, **kw: False
    alerts.send_alert = lambda *a, **kw: False
    return cal


def _route(row: ProcessedEmail | None, passed_filter: bool) -> str:
    if row is None:  # never claimed: either the pre-filter dropped it, or the daily budget held it back
        return "deferred" if passed_filter else "filtered_out"
    if row.extraction_action_type and row.extraction_action_type.value in ("needs_reply", "unclear"):
        return "action_item"
    return {"added": "auto_scheduled", "needs review": "needs_review"}.get(display_status(row), display_status(row))


def run(keep: bool = False, redo: tuple[str, ...] = ()) -> None:
    """A clean demo by default. With keep, finished emails stay as they are (no new Gemini calls for them)
    and only unfinished ones, plus any listed in redo, go through the pipeline again."""
    install_fakes()
    raw_by_id: dict[str, dict] = {}
    if keep:
        Base.metadata.create_all(engine)
        if TRACE_PATH.exists():  # Gemini's earlier answers, for the emails that are not run again
            raw_by_id = {e["id"]: e["extraction"] for e in json.loads(TRACE_PATH.read_text())["emails"] if e["extraction"]}
        with get_session() as s:
            for email_id in redo:
                if (row := s.get(ProcessedEmail, email_id)) is not None:
                    s.delete(row)
                raw_by_id.pop(email_id, None)
            s.commit()
    else:
        Base.metadata.drop_all(engine)  # only ever the demo database, checked above
        Base.metadata.create_all(engine)

    real_parse = llm_client.parse_llm_response

    def recording_parse(text: str):
        data = json.loads(text)
        raw_by_id[data.get("email_id", "?")] = data
        return real_parse(text)

    llm_client.parse_llm_response = recording_parse

    # Gemini's 503 "high demand" errors come and go; the pipeline leaves such an email failed and retries it
    # on its next run, so do what the hourly schedule would, just a minute apart instead of an hour.
    for attempt in range(1, 4):  # capped: Gemini's daily quota is shared with the real hourly pipeline
        result = pipeline.run_pipeline()
        print(f"run {attempt}:", {k: result[k] for k in ("fetched", "processed", "failed", "filtered_out")})
        if not result["failed"]:
            break
        time.sleep(60)

    session = get_session()
    trace = []
    for e in FAKE_EMAILS:
        row = session.get(ProcessedEmail, e.id)
        signals = score_email(e.subject, e.body_text)
        passed = is_actionable_candidate(e.subject, e.body_text)
        trace.append({
            "id": e.id,
            "sender": e.sender.split(" <")[0],
            "subject": e.subject,
            "snippet": " ".join(e.body_text.split())[:110],
            "filter": {"signals": signals, "passed": passed},
            "extraction": raw_by_id.get(e.id),
            "route": _route(row, passed),
            "deadline": row.extraction_deadline_parsed.isoformat() if row and row.extraction_deadline_parsed else None,
            "has_time": bool(row and row.extraction_has_time),
        })
        print(f"{e.id}  {trace[-1]['route']:<15} {e.subject}")
    session.close()

    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRACE_PATH.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "emails": trace}, indent=2))
    print("trace ->", TRACE_PATH)


def serve(port: int = 8002) -> None:
    """The real dashboard over a fresh copy of the demo database (<name>_live), so a recording that clicks
    Approve can be redone from the same starting point: every `serve` starts from what `run` produced."""
    from sqlalchemy import create_engine, text

    source = urlparse(DEMO_URL).path.lstrip("/")
    if source.endswith("_live"):  # the restarted process below: the copy is ready, just serve it
        import uvicorn

        install_fakes()
        from app.main import app

        print(f"demo dashboard on http://localhost:{port} (database {source})", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=port)
        return

    live = f"{source}_live"
    engine.dispose()  # CREATE DATABASE ... TEMPLATE needs no open connections to the source
    base = DEMO_URL.rsplit("/", 1)[0]
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{live}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{live}" TEMPLATE "{source}"'))
    admin.dispose()
    # Restart pointed at the copy, so every module picks it up at import time like a normal start.
    os.execve(sys.executable, [sys.executable, "-m", "scripts.demo_video", "serve"],
              {**os.environ, "DEMO_DATABASE_URL": f"{base}/{live}"})


if __name__ == "__main__":
    command, flags = (sys.argv[1] if len(sys.argv) > 1 else "run"), sys.argv[2:]
    if command == "run":
        # run [--keep] [--redo demo-06,demo-02]
        redo = tuple(flags[flags.index("--redo") + 1].split(",")) if "--redo" in flags else ()
        run(keep="--keep" in flags or bool(redo), redo=redo)
    else:
        serve()
