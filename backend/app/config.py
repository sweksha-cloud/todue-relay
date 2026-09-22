import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BACKEND_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_DIR = BACKEND_DIR / "credentials"

# Gmail: read almost everything, and the one write op this project uses (moving a message to
# Trash — recoverable there for 30 days, same as Gmail's own trash icon; never a permanent,
# bypass-Trash delete). Widened from gmail.readonly on 2026-09-22 specifically for the dashboard's
# "move to trash" button — see docs/design-decisions.md, decision 28, for the trade-off.
# + Calendar events write (create/update events only, not full calendar management).
# One combined OAuth flow/token so you only consent once.
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]

GMAIL_CLIENT_SECRET_PATH = Path(
    os.getenv("GMAIL_CLIENT_SECRET_PATH", CREDENTIALS_DIR / "client_secret.json")
)
GMAIL_TOKEN_PATH = Path(
    os.getenv("GMAIL_TOKEN_PATH", CREDENTIALS_DIR / "gmail_token.json")
)

# Pre-filter aggressiveness — see docs/design-decisions.md, decision 8 for the reasoning.
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
# See docs/design-decisions.md, decision 2.
STALE_CLAIM_MINUTES = int(os.getenv("STALE_CLAIM_MINUTES", "3"))

# Single-flight guard (repository.try_start_run, called by pipeline.run_pipeline):
# a non-dry run exits immediately if another run's RUNNING row started within
# this many minutes, so a manual run can't overlap a scheduled one (or Actions
# overlap Lambda) and double-spend the per-day Gemini budget, which only counts
# *finished* runs. It is also a lease: a run that dies without finishing (Lambda's
# 15-minute cap, Actions' 25-minute cap, a killed process) leaves a RUNNING row
# that stops blocking once it is this old. Must exceed the longest legitimate run.
RUN_LOCK_TTL_MINUTES = int(os.getenv("RUN_LOCK_TTL_MINUTES", "30"))

# Retry cap for an email whose extraction keeps FAILING (an error caught
# per-email, e.g. an unparseable date or a bad LLM response). Without a cap,
# a deterministic failure re-calls Gemini every run forever. Once a FAILED
# row's attempt_count reaches this, it is no longer reclaimed — it stays
# FAILED (still visible on the dashboard, error message intact), just no
# longer retried. Lowered 5 -> 3 on 2026-09-18: the free-tier Gemini quota is
# only 20 requests/day, so one bad email at 5 attempts costs 25% of a day's
# allowance, at 3 it costs 15%. 3 attempts still rides out ~11h of outage at
# GitHub's real ~3.7h schedule cadence (the cron is nominally hourly but
# isn't). 429s don't count toward this (see llm_client.LLMRateLimitError);
# 5xx errors do. Does not cap the stale-PROCESSING (worker hard-crash)
# reclaim — see repository.try_claim_email.
MAX_ATTEMPTS_PER_EMAIL = int(os.getenv("MAX_ATTEMPTS_PER_EMAIL", "3"))

# A failure that isn't the email's fault (Gemini's 429 quota errors, its 5xx "high demand" errors, a
# dropped connection) is refunded, so an outage cannot use up an email's attempts and park it for good.
# But that refund needs a bound, or an email that triggers a *permanent* server error would be retried
# every hour forever (the recovery sweep re-fetches FAILED rows by id, ignoring the fetch window). So the
# refund only applies while the email is younger than this. Past it, a failure counts like any other and
# the cap takes over. Default matches the 2-day fetch window.
# See docs/design-decisions.md, decision 25.
TRANSIENT_RETRY_WINDOW_HOURS = int(os.getenv("TRANSIENT_RETRY_WINDOW_HOURS", "48"))

# An SNS topic the pipeline emails when an email is parked for good (it failed MAX_ATTEMPTS_PER_EMAIL
# times and will not be retried automatically). On AWS it is the same topic the CloudWatch alarms use,
# set by the SAM template. Empty (local runs, GitHub Actions) means no alert is sent, only logged.
# See docs/design-decisions.md, decision 26.
ALERT_TOPIC_ARN = os.getenv("ALERT_TOPIC_ARN", "").strip()

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
# docs/design-decisions.md, decision 11 for the full reasoning. Decided 2026-09-14.
FETCH_WINDOW_DAYS = int(os.getenv("FETCH_WINDOW_DAYS", "2"))

# Plausibility bounds for a parsed deadline. Decided 2026-09-14 — see
# docs/design-decisions.md, decision 10. A date failing this
# check never gets silently dropped: it's always routed to "needs review"
# with a distinct "implausible" flag (see ProcessedEmail.is_implausible_date),
# never auto-rejected.
PLAUSIBLE_MAX_PAST_DAYS = int(os.getenv("PLAUSIBLE_MAX_PAST_DAYS", "3"))
PLAUSIBLE_MAX_FUTURE_DAYS = int(os.getenv("PLAUSIBLE_MAX_FUTURE_DAYS", "365"))

# Duplicate-deadline matching (docs/design-decisions.md, decision 5,
# decided 2026-09-15): two extractions are treated as the same underlying
# deadline if their normalized event names are at least this similar
# (difflib.SequenceMatcher ratio, 0-1). Placeholder, not yet tuned against
# real data — same "needs a real tuning pass" status the Step 1 pre-filter
# started at before tune_filter.py was run against a real inbox.
DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD = float(
    os.getenv("DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD", "0.7")
)

# Observability layer (2026-09-18, app/metrics.py). A run's filter-pass-rate
# is flagged as anomalous if it deviates from the trailing-7-run average by
# more than this many percentage points (0-1 scale) — e.g. 0.25 = a 25pt
# swing. Untuned placeholder, same "pick something reasonable, revisit with
# real data" status as DUPLICATE_EVENT_NAME_SIMILARITY_THRESHOLD above.
FILTER_PASS_RATE_ANOMALY_THRESHOLD = float(
    os.getenv("FILTER_PASS_RATE_ANOMALY_THRESHOLD", "0.25")
)
# Minimum emails a run must have actually offered to the filter (fetched
# minus already-terminal) before its pass rate is even eligible to be
# flagged — a 1-email run passing or failing is 0%/100% by pure chance,
# not a signal.
FILTER_ANOMALY_MIN_SAMPLE_SIZE = int(os.getenv("FILTER_ANOMALY_MIN_SAMPLE_SIZE", "5"))

# Gemini free-tier requests-per-DAY cap (per project, per model), resetting
# at midnight Pacific. CONFIRMED 2026-09-18 from Google AI Studio's Rate
# limit page for this account: gemini-3.6-flash RPD 20, RPM 5, TPM 250K
# (matches the stored per-minute 429s, quotaValue 5). Google's docs list no
# monthly cap. Limits are per project AND per model: anything else using the
# same project/model (AI Studio playground, another script) spends this same
# allowance and is invisible to this pipeline's own count.
# See docs/design-decisions.md, decision 12.
GEMINI_DAILY_QUOTA = int(os.getenv("GEMINI_DAILY_QUOTA", "20"))

# Per-day call budget guard (2026-09-18, pipeline.run_pipeline): a run stops
# claiming new emails once today's Gemini calls reach GEMINI_DAILY_QUOTA minus
# this reserve, leaving the rest unclaimed for a later run instead of burning
# calls into 429s. The reserve is a small cushion for calls this pipeline
# can't see (the AI Studio playground, another script on the same project —
# ~5 unexplained calls were seen on 2026-09-18). Untuned. Setting
# GEMINI_DAILY_QUOTA=0 turns the guard off; raising it (e.g. for a one-off
# manual run on a paid key) loosens it.
GEMINI_DAILY_RESERVE = int(os.getenv("GEMINI_DAILY_RESERVE", "2"))

# Surfaced on the main dashboard (2026-09-18), not just /metrics: flag
# usage as a warning once it crosses this % of GEMINI_DAILY_QUOTA, so a
# creeping approach to the limit is noticed before the pipeline starts
# failing on 429s.
LLM_USAGE_WARNING_THRESHOLD_PCT = float(os.getenv("LLM_USAGE_WARNING_THRESHOLD_PCT", "80"))

# Gmail add-on API (app/addon_app.py). The add-on runs on Google's servers and sends a Google
# identity token with each request (Apps Script: ScriptApp.getIdentityToken()). The API accepts a
# request only if the token is genuine, was issued for THIS client id, and belongs to this one
# email address. Both must be set; with either empty the API refuses everything (fail closed).
# The client id is the "aud" claim of a token the add-on obtains: see addon/README.md.
ADDON_OAUTH_CLIENT_ID = os.getenv("ADDON_OAUTH_CLIENT_ID", "").strip()
ADDON_ALLOWED_EMAIL = os.getenv("ADDON_ALLOWED_EMAIL", "").strip().lower()
