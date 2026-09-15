import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BACKEND_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_DIR = BACKEND_DIR / "credentials"

# Gmail read-only (never modifies/deletes mail) + Calendar events write
# (create/update events only, not full calendar management). One combined
# OAuth flow/token so you only consent once.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

GMAIL_CLIENT_SECRET_PATH = Path(
    os.getenv("GMAIL_CLIENT_SECRET_PATH", CREDENTIALS_DIR / "client_secret.json")
)
GMAIL_TOKEN_PATH = Path(
    os.getenv("GMAIL_TOKEN_PATH", CREDENTIALS_DIR / "gmail_token.json")
)

# Pre-filter aggressiveness — see claude/tradeoffs/pre-filter-aggressiveness.md for the reasoning.
FILTER_LEVEL = os.getenv("FILTER_LEVEL", "moderate")

# Extraction LLM: Gemini. Key from https://aistudio.google.com — not committed.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Free tier for this model allows 5 requests/minute per project (observed
# directly: a real run hit 429 RESOURCE_EXHAUSTED repeatedly with no
# pacing). 13s keeps us under that with margin. Step 5's "account for rate
# limits" requirement, not a tuning knob.
GEMINI_MIN_INTERVAL_SECONDS = float(os.getenv("GEMINI_MIN_INTERVAL_SECONDS", "13"))

# Postgres connection string (Neon/Supabase). Not set until the DB is provisioned.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# How long a "processing" claim is honored before another run is allowed to
# retry it — covers the crash-mid-batch case. Decided 2026-09-15: 3 — real
# GitHub Actions logs show a single Gemini extraction takes ~1s, so 3
# minutes is still 6-10x the realistic worst-case per-email time, but
# recovers from a crash 5x faster than the original 15-minute guess.
# See claude/tradeoffs/stale-claim-minutes.md.
STALE_CLAIM_MINUTES = int(os.getenv("STALE_CLAIM_MINUTES", "3"))

# Length of a timed Calendar event when the deadline has an explicit time.
# Decided 2026-09-15: 0 — a point-in-time marker exactly at the deadline
# ("due at 5" -> an event at 5, not a 5:00-5:30 block). A date-only
# deadline becomes an all-day event instead (unaffected by this value).
EVENT_DURATION_MINUTES = int(os.getenv("EVENT_DURATION_MINUTES", "0"))

# IANA timezone (e.g. "America/New_York") for timed Calendar events, which
# require one explicitly. Empty means auto-detect from the machine's own
# /etc/localtime; set this to override.
CALENDAR_TIMEZONE = os.getenv("CALENDAR_TIMEZONE", "")

# Step 5 rate-limit guardrail: bound how much one run can touch, regardless
# of inbox size. Gmail/Calendar/Gemini quotas are all far above what this
# project needs (verified), so this is just a sanity cap, not a quota
# workaround.
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", "50"))
# Wider than the run cadence on purpose: this is a resilience margin
# against gaps in execution (a crash, an outage, a missed restart), not
# "how far back to normally look." A window equal to the cadence would
# mean any downtime longer than that permanently loses emails — see
# claude/tradeoffs/fetch-window.md for the full reasoning. Decided 2026-09-14.
FETCH_WINDOW_DAYS = int(os.getenv("FETCH_WINDOW_DAYS", "2"))

# Plausibility bounds for a parsed deadline. Decided 2026-09-14 — see
# claude/tradeoffs/stale-implausible-date-handling.md. A date failing this
# check never gets silently dropped: it's always routed to "needs review"
# with a distinct "implausible" flag (see ProcessedEmail.is_implausible_date),
# never auto-rejected.
PLAUSIBLE_MAX_PAST_DAYS = int(os.getenv("PLAUSIBLE_MAX_PAST_DAYS", "3"))
PLAUSIBLE_MAX_FUTURE_DAYS = int(os.getenv("PLAUSIBLE_MAX_FUTURE_DAYS", "365"))

# Duplicate-deadline matching (claude/tradeoffs/duplicate-deadline-detection.md,
# decided 2026-09-15): two extractions are treated as the same underlying
# deadline if their normalized event names are at least this similar
# (difflib.SequenceMatcher ratio, 0-1). Placeholder, not yet tuned against
# real data — same "needs a real tuning pass" status the Step 1 pre-filter
# started at before tune_filter.py was run against a real inbox.
DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD = float(
    os.getenv("DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD", "0.7")
)
