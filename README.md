# ToDue Relay

Scans your Gmail for deadlines and action items (RSVPs, interview
scheduling requests, etc.), extracts them with an LLM, and syncs
high-confidence deadlines straight to Google Calendar. Uncertain
extractions land in a review dashboard instead of being guessed at.

Full design history — every tradeoff considered and why, plus what's
intentionally deferred — lives in `claude/` (gitignored, local-only):
`claude/claude-code-build-prompt.md` (original spec), `claude/tradeoffs/`,
`claude/post-prod/`, `claude/review.md`.

## How it works

1. **Fetch** unread, recent Gmail messages (`app/gmail_client.py`)
2. **Pre-filter** cheaply before any LLM call (`app/filters.py`)
3. **Extract** structured deadline/action data via Gemini (`app/llm_client.py`)
   — the free tier allows only **20 calls/day**, see [Gemini quota](#gemini-quota)
4. **Route**: high-confidence deadlines auto-create a Calendar event
   (a true recurring event if the email describes a repeating obligation,
   e.g. "rent due the 1st of every month"); a deadline recognized as a
   repeat or update of one already tracked skips creating a duplicate, or
   moves the existing event if the date changed; low confidence or
   no-fixed-date items go to a review dashboard (`app/pipeline.py`)
5. **Track** every email's outcome in Postgres for idempotency and audit
   (`app/db/`) — safe to re-run, never double-processes or double-creates
6. **Review** via a small dashboard (`app/main.py` + `app/templates/`), plus a
   `/metrics` page (run history, filter pass rate, Gemini usage vs. the daily
   quota) and a **Check waiting mail** button that counts unprocessed emails
   on demand — read-only, costs no Gemini quota
7. **Protect the quota**: a per-day call budget stops claiming emails once the
   day's allowance is spent, and a retry cap stops a failing email from being
   retried forever (`app/pipeline.py`, `app/db/repository.py`)

## Setup

### 1. Google Cloud — Gmail + Calendar access

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Gmail API** and **Google Calendar API**
3. Configure the OAuth consent screen (External user type; add your own
   email as a test user — this keeps refresh tokens working past 7 days
   only if you re-authenticate periodically; see note below)
4. Create an OAuth Client ID, type **Desktop app**
5. Download the JSON, save as `backend/credentials/client_secret.json`

**Note on OAuth "Testing" status:** while unverified, refresh tokens
expire after 7 days, requiring you to re-approve access in a browser.
Gmail scopes are Google-"restricted," so moving to full verified
production status requires a security review — not worth it for personal
use. Just expect to re-auth periodically, or script a reminder.

### 2. Gemini API key

Get one from [aistudio.google.com](https://aistudio.google.com).

### 3. Postgres

Any real Postgres instance works — this project runs on [Neon](https://neon.tech)
(free, scales to zero, auto-wakes with no manual restore step — see
`claude/design-choices-defense/database-provider-choice.md` for why Neon
specifically over Supabase/Render/Railway/self-hosted). A local instance
works fine for development too:

```bash
docker run -d --name deadline-tracker-pg -e POSTGRES_PASSWORD=<pw> \
  -e POSTGRES_DB=deadlines -p 55432:5432 postgres:16-alpine
```

The OAuth token, once obtained, is stored in this database (not just a
local file) so it survives on ephemeral compute like GitHub Actions —
see `app/google_auth.py`.

**Schema changes are manual.** There's no migration tool: the dashboard's
startup runs `create_all`, which creates missing tables but never adds columns
to an existing one. A database created before the observability counters were
added needs them by hand (safe to re-run):

```sql
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS emails_filtered_out INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS emails_already_terminal INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS emails_deferred INTEGER NOT NULL DEFAULT 0;
```

Run these **before** deploying code that uses a new column; the `DEFAULT 0`
keeps older code working in the meantime.

### 4. Configure

```bash
cd backend
cp .env.example .env
# fill in GEMINI_API_KEY, DATABASE_URL, CALENDAR_TIMEZONE (your IANA zone,
# e.g. America/Los_Angeles — required, see .env.example for why)
```

### 5. Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the pipeline once (opens a browser for Google consent on first run)
python -m scripts.run_pipeline

# See what a run WOULD do — spends no Gemini quota and writes nothing.
# Prefer this for checking the pipeline: the free tier is only 20 Gemini calls/day.
python -m scripts.run_pipeline --dry-run

# Run the dashboard
uvicorn app.main:app --port 8000
# -> http://localhost:8000
```

### Tuning the pre-filter

Before trusting the pre-filter against your real inbox, see how it
performs:

```bash
python -m scripts.tune_filter --max-results 200
```

Run against a real inbox (79 unread emails, 2026-09-15): `moderate`
passed ~39% through to the LLM, `strict` ~5%, `loose` ~66%. Full
breakdown and the reasoning for keeping `moderate`:
`claude/tradeoffs/pre-filter-aggressiveness.md`.

## Gemini quota

The free tier of `gemini-3.6-flash` allows **20 requests/day** (and 5/minute),
per project and model — confirmed on Google AI Studio's Rate limit page. The day
resets at **midnight Pacific**; there is no monthly cap. A dashboard card and
`/metrics` show today's calls against it. Real runs, retries and manual runs all
spend the same 20, so the pipeline protects it in layers:

- **Per-day call budget** — once today's calls reach `GEMINI_DAILY_QUOTA` minus
  `GEMINI_DAILY_RESERVE`, a run stops claiming emails. Those emails are
  **deferred**: never claimed, left untouched, and picked up by a later run after
  the reset. The dashboard banner shows how many are waiting.
- **Retry cap** — an email that keeps failing is retried at most
  `MAX_ATTEMPTS_PER_EMAIL` times, then stays visibly `failed`. Rate-limit (429)
  errors don't count toward it.
- **Dry run** — check what a run *would* do without spending anything:
  `python -m scripts.run_pipeline --dry-run` (or the dashboard button). It reads
  Gmail and the database and writes nothing. Use it instead of a real run to
  verify changes.

| Setting | Default | Meaning |
|---|---|---|
| `GEMINI_DAILY_QUOTA` | `20` | Your daily request limit (`0` turns the budget guard off) |
| `GEMINI_DAILY_RESERVE` | `2` | Calls held back for usage the pipeline can't see |
| `MAX_ATTEMPTS_PER_EMAIL` | `3` | Retries before a failing email is left alone |
| `GEMINI_MIN_INTERVAL_SECONDS` | `13` | Pacing to stay under 5 requests/minute |

On a paid tier, raise `GEMINI_DAILY_QUOTA`. To force one bigger run, set it for
that run only, e.g. `GEMINI_DAILY_QUOTA=100 python -m scripts.run_pipeline`.

## Testing

```bash
cd backend
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql+psycopg://postgres:test@localhost:55432/testdb \
  python -m pytest tests/ -v
```

150 tests: date/timezone parsing, pre-filter scoring, LLM response
validation (including 429 handling), Calendar event construction (including
recurrence), duplicate-deadline matching, the idempotency claim logic and retry
cap, the daily call budget and dry-run mode (driving the real `run_pipeline`
loop with only Gmail and Gemini stubbed), the observability metrics, and the
dashboard pages rendered against real Postgres. The claim logic uses
`ON CONFLICT ... RETURNING`, which has no SQLite equivalent, so a real
Postgres instance is required — `TEST_DATABASE_URL` points at one, separate
from the app's own `DATABASE_URL`. Runs automatically on every push via
`.github/workflows/tests.yml` (a Postgres service container, no local
setup needed in CI).

## Deployment

Runs on a schedule via GitHub Actions (`.github/workflows/pipeline.yml`)
— every hour, plus manual trigger (`workflow_dispatch`). Needs two repo
secrets set (`gh secret set GEMINI_API_KEY` / `DATABASE_URL`, or via the
GitHub UI under Settings → Secrets and variables → Actions):

- `GEMINI_API_KEY`
- `DATABASE_URL`

`CALENDAR_TIMEZONE`, `FILTER_LEVEL`, and `FETCH_WINDOW_DAYS` are set
directly in the workflow file (not secrets, since they're not
sensitive). See `claude/tradeoffs/cron-interval.md` and
`claude/tradeoffs/fetch-window.md` for why those specific values.

**Cadence:** the schedule says hourly, but GitHub treats scheduled workflows as
best-effort — real runs were observed every 2.5-5.7 hours (about 3.7 on
average). Correctness doesn't depend on it (processing is idempotent and the
2-day fetch window tolerates gaps), but expect hours, not minutes, between
runs. Running less often wouldn't save Gemini quota anyway: each email is
processed once however often the job runs.

A second workflow (AWS Lambda + EventBridge) is planned as a deliberate
future migration once this has run for real for a while — not built
yet. See `claude/post-prod/aws-lambda-deployment.md`.

## Project layout

```
.github/workflows/
  pipeline.yml                      # Step 8: hourly + manual-trigger run
  tests.yml                          # CI: runs the test suite on every push
backend/
  app/
    gmail_client.py      # Step 1: fetch
    filters.py            # Step 1: pre-filter
    llm_client.py          # Step 2: Gemini extraction (rate-limited)
    schemas.py              # Step 2: structured-output contract
    date_utils.py             # timezone-aware date parsing (see comments —
                               #   this file has eaten more real bugs than
                               #   anything else in the project)
    google_auth.py             # shared Gmail+Calendar OAuth, DB-backed token
    db/                        # Step 3: idempotency + audit trail (Postgres)
    calendar_client.py          # Step 4: Calendar event creation/update/delete
    pipeline.py                  # Step 5: orchestrates all of the above
    main.py                       # Step 6/7: FastAPI + dashboard
    view_helpers.py                # dashboard display/badge logic
    metrics.py                     # observability: run history, usage vs. daily quota
    templates/                     # dashboard HTML (Jinja2 + htmx)
  scripts/
    run_pipeline.py                # entry point (also what CI schedules); --dry-run to preview
    tune_filter.py                  # pre-filter tuning against real inbox
  tests/                             # 150 tests, see Testing section above
```

## Status

Steps 1-8 built, tested, and verified against a real inbox, real Gemini
calls, real Calendar events (including true recurring events and
duplicate-deadline handling), and real GitHub Actions runs. An automated
test suite (150 tests) and CI run on every push. A security review pass
is complete (dependency CVEs patched, workflow permissions restricted,
XSS/injection risk checked directly, no secrets in git history) — one
accepted gap: the dashboard has no authentication, fine while run
locally, needs addressing before any public hosting.

Runs within Gemini's free tier (20 requests/day) via a per-day call budget,
a retry cap, and a read-only dry-run mode — see [Gemini quota](#gemini-quota).

Correctly not built yet, per the project's own plan: Step 9 (Redis-backed
queue, explicitly deferred until the simpler version has run for real),
the AWS Lambda migration (deliberate second phase, not started), and
everything in `claude/post-prod/`.

Full decision history: `claude/review.md` (current status),
`claude/tradeoffs/` (every decision made, with reasoning),
`claude/fine-tuning-todo.md` (known placeholder values still open).
