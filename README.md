# ToDue Relay

[![Tests](https://github.com/sweksha-cloud/todue-relay/actions/workflows/tests.yml/badge.svg)](https://github.com/sweksha-cloud/todue-relay/actions/workflows/tests.yml)

Scans your Gmail for deadlines and action items (RSVPs, interview scheduling
requests, "rent is due the 1st"), extracts them with an LLM, and syncs the
confident ones straight to Google Calendar. Anything uncertain lands in a review
dashboard instead of being guessed at.

**Built with:** Python, FastAPI, Jinja2 + htmx, PostgreSQL (SQLAlchemy 2),
Google Gemini, the Gmail and Calendar APIs, AWS Lambda + EventBridge Scheduler,
GitHub Actions (CI, and CD through GitHub OIDC). The scheduled job runs on AWS; see
[Deployment](#deployment).

## How it works

```mermaid
flowchart LR
    T1["EventBridge Scheduler (hourly) -> AWS Lambda"] --> P["run_pipeline"]
    T2["GitHub Actions (manual / fallback)"] -.-> P
    P --> G[("Gmail (read-only)")]
    G --> F{"Pre-filter"}
    F -- "not a candidate" --> X["skipped, no LLM call"]
    F -- "candidate" --> L["Gemini extraction (schema-validated)"]
    L --> R{"Confidence routing"}
    R -- "high confidence + plausible date" --> C["Google Calendar"]
    R -- "low / no date / implausible" --> Q["Review queue"]
    P <--> DB[("Postgres: claims, audit trail, OAuth token")]
    Q --> D["Dashboard: FastAPI + htmx"]
    DB --> D
```

1. **Fetch** unread, recent Gmail messages (`app/gmail_client.py`). A real
   calendar invite is skipped: Gmail and Calendar already handle those natively.
2. **Pre-filter** with a cheap scoring pass before any LLM call (`app/filters.py`).
3. **Extract** structured data with Gemini, constrained to a schema and
   validated on the way back (`app/llm_client.py`, `app/schemas.py`).
4. **Route** (`app/pipeline.py`): a high-confidence deadline with a plausible date
   creates a Calendar event, a true recurring one if the email describes a
   repeating obligation. A repeat of something already tracked is linked rather
   than duplicated, and a moved deadline updates the existing event in place.
   Everything else (low confidence, an implausible date, or no fixed date such
   as "can you schedule an interview?") goes to the review queue or the Action
   Items list.
5. **Track** every email's outcome in Postgres (`app/db/`) for idempotency and an
   audit trail. It is safe to re-run: nothing is processed or created twice.
6. **Review** on a small dashboard (`app/main.py`), where emails are grouped by the
   decision made about them (needs your review, on your calendar to check, marked
   correct or incorrect, denied or skipped, failed): approve, reschedule or deny held-back
   items, schedule an action item onto the calendar, correct, reschedule or remove
   an auto-created event (a removed item leaves the list), and see run
   history, filter pass rate and Gemini usage on `/metrics`. A **Check waiting
   mail** button counts unprocessed emails on demand, read-only and free of
   Gemini quota.

## Engineering highlights

| Problem | What the code does | Where |
|---|---|---|
| A crash or a re-run must never double-create a Calendar event | Atomic claim per email using Postgres `INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING`; a stale claim from a crashed run is reclaimed after `STALE_CLAIM_MINUTES`; a recovery sweep re-fetches stuck emails directly by id | `app/db/repository.py`, `app/pipeline.py` |
| Two runs at once (a manual run during a scheduled one, or Actions and Lambda together) would double-spend the daily Gemini budget | A single-flight guard: a Postgres advisory *transaction* lock serializes the check-and-insert of a `RUNNING` row, so a real run exits immediately if another started within `RUN_LOCK_TTL_MINUTES` and hasn't finished. The expiry doubles as a lease for crashed runs, and the lock is transaction-scoped, so it works through a pooled connection. Covered by a multi-connection race test | `app/db/repository.py`, `app/pipeline.py`, `tests/test_run_guard.py` |
| The LLM is slow, rate-limited and sometimes wrong | A cheap pre-filter runs first (on a real 79-email inbox, the default level passed ~39% to the LLM). Responses are validated against a schema. One bad email is isolated and recorded as failed without aborting the batch | `app/filters.py`, `app/schemas.py` |
| An unsure model must not silently write to your calendar | Confidence routing: only high confidence with a plausible date auto-creates. An implausible date is never dropped; it is flagged and queued | `app/pipeline.py` |
| The same deadline arrives twice, or moves | Name-similarity matching against tracked deadlines: always the same day, and across days only when the email has reschedule wording. A match with a changed date updates the existing event | `app/db/repository.py` |
| Timezones silently shift real events | Explicit `CALENDAR_TIMEZONE` (never the runner's clock), offsets attached to event times, and regression tests for every bug found in real data (`EST/EDT`, spelled-out regions, `GMT+2` sign inversion) | `app/date_utils.py`, `tests/test_date_utils.py` |
| The free Gemini tier allows 20 calls a day | A per-day budget (resets midnight Pacific), a retry cap where Gemini's 429 and 5xx errors don't count against an email (for a bounded time), and a `--dry-run` that spends nothing. See [Gemini quota](#gemini-quota) | `app/pipeline.py`, `app/metrics.py` |
| CI runners and Lambda have no persistent disk | The Google OAuth token lives in Postgres, not a local file; the local copy is only a best-effort dev mirror | `app/google_auth.py` |
| Least privilege | Gmail is `gmail.modify` (needed for the dashboard's "move to trash" button; the scheduled pipeline itself only ever reads); Calendar access is events-only; workflows run with `contents: read` | `app/config.py`, `.github/workflows/` |

The reasoning behind these, with the alternatives considered and the evidence, is in
[docs/design-decisions.md](docs/design-decisions.md).

## Setup

### 1. Google Cloud: Gmail and Calendar access

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com).
2. Enable the **Gmail API** and **Google Calendar API**.
3. Configure the OAuth consent screen (External user type) and add your own
   email as a test user.
4. Create an OAuth Client ID of type **Desktop app**.
5. Download the JSON and save it as `backend/credentials/client_secret.json`
   (gitignored).

While the app is unverified ("Testing" status), Google expires refresh tokens
after 7 days and you re-approve in a browser. Gmail scopes are "restricted", so
full verification needs a security review, which isn't worth it for personal use.

### 2. Gemini API key

Get one from [aistudio.google.com](https://aistudio.google.com).

### 3. Postgres

Any Postgres works. This project runs on [Neon](https://neon.tech) (free, scales
to zero, wakes automatically). For local development:

```bash
docker run -d --name todue-relay-pg -e POSTGRES_PASSWORD=<pw> \
  -e POSTGRES_DB=deadlines -p 5432:5432 postgres:16-alpine
```

**Use the `postgresql+psycopg://` URL scheme**, e.g.
`postgresql+psycopg://postgres:<pw>@localhost:5432/deadlines`. Neon and most
providers hand out plain `postgresql://` URLs, which SQLAlchemy sends to the
`psycopg2` driver; this project uses `psycopg` 3, so a plain URL fails with
`No module named 'psycopg2'`.

**Schema changes are manual.** There is no migration tool: startup runs
`create_all`, which creates missing tables but never adds columns to an existing
one. A database created before the observability counters were added needs them
by hand (safe to re-run):

```sql
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS emails_filtered_out INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS emails_already_terminal INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS emails_deferred INTEGER NOT NULL DEFAULT 0;
```

Run these before deploying code that uses a new column; `DEFAULT 0` keeps older
code working in the meantime.

### 4. Configure

```bash
cd backend
cp .env.example .env
# Fill in GEMINI_API_KEY, DATABASE_URL, and CALENDAR_TIMEZONE (an IANA zone such
# as America/Los_Angeles; required, because the machine's own zone is wrong on
# any runner that isn't your laptop).
```

`.env.example` lists every setting with its default, and a test keeps it in sync
with `app/config.py`.

### 5. Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the pipeline once (opens a browser for Google consent on first run)
python -m scripts.run_pipeline

# Preview what a run WOULD do: spends no Gemini quota and writes nothing.
python -m scripts.run_pipeline --dry-run

# Run the dashboard
uvicorn app.main:app --port 8000     # -> http://localhost:8000
```

Prefer `--dry-run` for checking changes; the free tier is only 20 Gemini calls a
day.

### Tuning the pre-filter

```bash
python -m scripts.tune_filter --max-results 200
```

Against a real inbox (79 unread emails), the default `moderate` level passed
~39% through to the LLM, `strict` ~5% and `loose` ~66%.

## Gemini quota

The free tier of `gemini-3.6-flash` allows **20 requests a day** (and 5 a
minute), per project and model. The day resets at **midnight Pacific**; there is
no monthly cap. The dashboard and `/metrics` show today's calls against it. Real
runs, retries and manual runs all spend the same 20, so the pipeline protects it
in layers:

- **Per-day call budget:** once today's calls reach `GEMINI_DAILY_QUOTA` minus
  `GEMINI_DAILY_RESERVE`, a run stops claiming emails. They are *deferred*:
  never claimed, left untouched, and picked up by a later run after the reset.
- **Retry cap:** an email that keeps failing is retried at most
  `MAX_ATTEMPTS_PER_EMAIL` times, then stays visibly `failed`. Errors that are not the
  email's fault (Gemini's 429 rate limits and 5xx "high demand" errors, a dropped connection) don't
  count toward it, but only while the email is younger than `TRANSIENT_RETRY_WINDOW_HOURS` (48), so a
  permanent server error cannot be retried forever. `python -m scripts.retry_failed` gives emails
  that an outage parked before this a fresh set of attempts. An email that does fail for good is
  called out in a banner on the dashboard and emailed once through the same alert topic as the
  CloudWatch alarms (decision 26).
- **Dry run:** shows what a run would do without spending anything
  (`--dry-run`, or the dashboard button).

| Setting | Default | Meaning |
|---|---|---|
| `GEMINI_DAILY_QUOTA` | `20` | Your daily request limit (`0` turns the budget guard off) |
| `GEMINI_DAILY_RESERVE` | `2` | Calls held back for usage the pipeline can't see |
| `MAX_ATTEMPTS_PER_EMAIL` | `3` | Retries before a failing email is left alone |
| `GEMINI_MIN_INTERVAL_SECONDS` | `13` | Pacing to stay under 5 requests a minute |

On a paid tier, raise `GEMINI_DAILY_QUOTA`. For one bigger run, set it for that
run only: `GEMINI_DAILY_QUOTA=100 python -m scripts.run_pipeline`.

## Testing

```bash
cd backend
pip install -r requirements-dev.txt

# A throwaway Postgres for the tests (separate from your app database):
docker run -d --name todue-relay-pg-test -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=testdb -p 55432:5432 postgres:16-alpine

TEST_DATABASE_URL=postgresql+psycopg://postgres:test@localhost:55432/testdb \
  python -m pytest tests/ -v
```

488 tests across 22 files (plus 113 for the Gmail add-on), covering date and timezone parsing, pre-filter
scoring, LLM response validation (including 429 and 5xx handling), Calendar event
construction (including recurrence), duplicate-deadline matching, the idempotency
claim logic and retry cap, the daily call budget and dry-run mode (driving the
real `run_pipeline` loop with only Gmail and Gemini stubbed), the metrics, the
dashboard pages rendered against real Postgres, the OAuth token save, and the
Lambda handler and its packaging. The claim logic uses `ON CONFLICT ... RETURNING`,
which has no SQLite equivalent, so the suite needs a real Postgres. It runs on
every push via `.github/workflows/tests.yml`, with a Postgres service container.

## Deployment

**Live: AWS Lambda + EventBridge Scheduler** (since 2026-09-20). An EventBridge
Scheduler schedule (`cron(0 * * * ? *)`: every hour, on the hour, UTC) invokes the
Lambda `todue-relay-sam-pipeline` (Python 3.14, arm64, 1024 MB, 15-minute timeout, no VPC).
`backend/lambda_handler.py` runs the same `run_pipeline`, reads its Gemini key and
database URL from AWS Secrets Manager, and accepts `{"dry_run": true}` for quota-free
checks. `aws/build_lambda.sh` packages it inside AWS's own Lambda Python image
(about 37 MB zipped, inside the 50 MB direct-upload limit). The Lambda, schedule, roles and alarms are
defined in `aws/template.yaml` (AWS SAM, stack `todue-relay-sam`). Deploys are automated: a push to `main`
that touches the Lambda's code or template runs the tests, builds the package, deploys the stack
with SAM and dry-run tests the function. GitHub authenticates to AWS with a short-lived OIDC token
(no AWS keys are stored), through two narrowly scoped roles defined in `aws/ci-template.yaml`.
`sam deploy` from `aws/` still works by hand.

```mermaid
flowchart LR
    subgraph AWS["AWS us-east-2, defined in aws/template.yaml (SAM)"]
        direction LR
        S["EventBridge Scheduler<br/>hourly, cron in UTC"] --> L["Lambda: run_pipeline<br/>Python 3.14, arm64"]
        SM[("Secrets Manager<br/>Gemini key, DB URL")] --> L
        L -. "metrics" .-> AL["2 CloudWatch alarms<br/>errors, not running"]
        AL --> SNS["SNS email"]
    end
    subgraph EXT["External APIs"]
        direction TB
        GM["Gmail, read-only"]
        GE["Gemini"]
        GC["Google Calendar"]
    end
    L --> EXT
    L <--> N[("Neon Postgres<br/>runs, claims, OAuth token")]
    N --> D["Dashboard: FastAPI + htmx<br/>runs locally"]
```

- **Least privilege.** The function's execution role can write one log group and read
  one secret. A separate scheduler role can only invoke the function. Both roles are
  defined in `aws/template.yaml`, and their policies were checked with IAM Access Analyzer
  before first use.
- **Failure containment.** No automatic retries (a retry would spend scarce Gemini
  quota) and a queued event is dropped after 60 seconds. A CloudWatch alarm emails on
  any error, and a second alarm fires if the function hasn't run for about 3 hours.
- **No overlapping runs.** The pipeline refuses to start while another run is in
  progress (the database-level guard described above), which covers a manual run during
  a scheduled one and Actions overlapping Lambda. Lambda's own "reserved concurrency"
  setting isn't available on a new AWS account, which is why the guard lives in the
  application.

**Why it moved off GitHub Actions:** the Actions schedule said hourly, but GitHub
treats scheduled workflows as best-effort, and real runs were observed every 2.5 to
5.7 hours (about 3.7 on average). Correctness never depended on it (processing is
idempotent and a 2-day fetch window tolerates gaps), but it wasn't the cadence intended.

**GitHub Actions is still used** for CI (`tests.yml`, on every push and pull request)
and for `.github/workflows/pipeline.yml`, now only a manual and fallback trigger
(`workflow_dispatch`). It needs two repository secrets, `GEMINI_API_KEY` and
`DATABASE_URL`, and is capped at 25 minutes with runs serialized. The old hourly cron
is kept in that file as a comment.

**The AWS deployment is time-boxed.** It runs on the AWS Free plan's credits, and the
plan is to move the schedule back to GitHub Actions before the free plan ends in March
2027 (disable the AWS schedule first, then restore the cron).

## Project layout

```
.github/workflows/
  pipeline.yml                  # manual / fallback pipeline run (the schedule is on AWS)
  tests.yml                     # CI: the test suite on every push
  deploy.yml                    # CD: tests, then SAM deploy to AWS through GitHub OIDC
aws/
  template.yaml                 # SAM template: Lambda, schedule, IAM roles, alarms
  ci-template.yaml              # the GitHub OIDC provider and the two deploy roles
  build_lambda.sh               # builds the Lambda zip (Docker)
backend/
  app/
    pipeline.py                 # orchestrates fetch > filter > extract > route > track
    gmail_client.py             # fetch, plus trash_message (the dashboard's one write op)
    filters.py                  # cheap pre-filter before any LLM call
    llm_client.py               # Gemini extraction, rate-limited
    schemas.py, prompts.py      # structured-output contract and prompt
    calendar_client.py          # Calendar event create / update / delete
    date_utils.py               # timezone-aware date parsing (the module with the
                                #   most real bugs found, and the most tests)
    google_auth.py              # shared Gmail + Calendar OAuth, DB-backed token
    db/                         # models, sessions, idempotency + audit (Postgres)
    metrics.py                  # run history, filter pass rate, usage vs. quota
    main.py, view_helpers.py    # FastAPI dashboard
    addon_app.py, addon_api.py, addon_auth.py  # the add-on's own authenticated API (not the dashboard)
    review_actions.py           # vote / approve / decline / remove, shared by the dashboard and the add-on
    categories.py               # the decision groups the dashboard sorts emails into
    templates/                  # dashboard HTML (Jinja2 + htmx)
    config.py                   # every setting, read from the environment
  scripts/
    run_pipeline.py             # entry point (what CI schedules); --dry-run
    tune_filter.py              # pre-filter tuning against a real inbox
  lambda_handler.py             # AWS Lambda entry point
  requirements.txt              # full app (pipeline + dashboard)
  requirements-lambda.txt       # pipeline-only, for the Lambda package
  requirements-dev.txt
  tests/                        # 488 tests, run against real Postgres
addon/                          # Gmail add-on (Apps Script, TypeScript): a read-only home card
docs/
  design-decisions.md           # 23 decisions: what else was considered, and the evidence
SCALING.md                      # how I'd extend it to multiple users (a design exercise)
```

## Status and limitations

Built, tested, and run against a real inbox, real Gemini calls, real Calendar
events (including recurring events and duplicate handling) and real GitHub
Actions and AWS Lambda runs. Dependencies were audited for known CVEs and upgraded, and text
extracted from email is HTML-escaped on the dashboard (tested with real script
payloads).

Known limitations:

- **The dashboard has no authentication.** It is meant to run locally; it needs
  auth before any public hosting.
- **Schema changes are manual** (no migration tool; see [Setup](#3-postgres)).
- **The free Gemini tier caps throughput** at 20 extractions a day, so a large
  backlog is worked off over several days.
- **The AWS hosting is time-boxed** to the free plan, which ends in March 2027
  (see [Deployment](#deployment)).
- **Google refresh tokens expire every 7 days** while the OAuth app is in Testing
  status.
- **Single-user by design.** It reads one Gmail account. See [SCALING.md](SCALING.md) for how I'd
  extend this to multiple users.

Not built: Google Tasks integration for dateless items and a queue-based worker (deferred until
the simple version has more real use). A Gmail add-on (a home card with review buttons, over an authenticated
API) is written and tested in [`addon/`](addon/README.md), including an end-to-end run against a real
API on sample data, but not yet installed in a real Gmail.
