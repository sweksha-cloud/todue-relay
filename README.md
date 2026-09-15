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
4. **Route**: high-confidence deadlines auto-create a Calendar event; low
   confidence or no-fixed-date items go to a review dashboard
   (`app/pipeline.py`)
5. **Track** every email's outcome in Postgres for idempotency and audit
   (`app/db/`) — safe to re-run, never double-processes or double-creates
6. **Review** via a small dashboard (`app/main.py` + `app/templates/`)

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

Any Postgres instance works — a hosted one (Neon/Supabase) for real use,
or local for development:

```bash
docker run -d --name deadline-tracker-pg -e POSTGRES_PASSWORD=<pw> \
  -e POSTGRES_DB=deadlines -p 55432:5432 postgres:16-alpine
```

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

## Project layout

```
backend/
  app/
    gmail_client.py      # Step 1: fetch
    filters.py            # Step 1: pre-filter
    llm_client.py          # Step 2: Gemini extraction
    schemas.py              # Step 2: structured-output contract
    date_utils.py             # timezone-aware date parsing (see comments —
                               #   this file has eaten more real bugs than
                               #   anything else in the project)
    db/                        # Step 3: idempotency + audit trail (Postgres)
    calendar_client.py          # Step 4: Calendar event creation
    pipeline.py                  # Step 5: orchestrates all of the above
    main.py                       # Step 6/7: FastAPI + dashboard
    templates/                     # dashboard HTML (Jinja2 + htmx)
  scripts/
    run_pipeline.py                # manual entry point
    tune_filter.py                  # pre-filter tuning against real inbox
```

## Status

Steps 1-7 built and verified against a real inbox, real Gemini calls, and
real Calendar events. Step 8 (scheduled deployment) not yet built — see
`claude/review.md` for current decisions made/open and
`claude/fine-tuning-todo.md` for known placeholder values and open gaps.
