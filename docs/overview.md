# Project overview

ToDue Relay reads one Gmail inbox on an hourly schedule, extracts deadlines and action items from
email with an LLM, and syncs the confident ones straight to Google Calendar. Anything uncertain is
held for review instead of being guessed at. Two surfaces let a person review it: a web dashboard
and a Gmail add-on. For the reasoning behind each choice, see
[design-decisions.md](design-decisions.md); for setup and usage, see the [README](../README.md).

## The pieces

| Stage | What it does | Where |
|---|---|---|
| Fetch | Pulls unread/recent Gmail messages; detects real calendar invites and Google Calendar's own notifications so they're never double-processed | `app/gmail_client.py` |
| Pre-filter | Cheap keyword/pattern scoring, before any LLM call | `app/filters.py` |
| Extract | Gemini call with a strict, schema-validated response | `app/llm_client.py`, `app/schemas.py` |
| Parse & judge dates | Free text → a real, timezone-aware datetime; flags implausible dates | `app/date_utils.py` |
| Route | High confidence + plausible date → auto-create; everything else → review queue or Action Items; duplicate/reschedule matching | `app/pipeline.py` |
| Sync to Calendar | Create/update/delete events, recurrence, idempotent deletes | `app/calendar_client.py` |
| Track | Every outcome recorded in Postgres — idempotent, crash-safe, auditable | `app/db/` |
| Review (web) | Server-rendered dashboard: categorized lists, approve/reschedule/remove/trash, `/metrics` | `app/main.py`, `app/templates/` |
| Review (Gmail) | A Gmail Workspace Add-on showing the same data and actions inside Gmail | `gmail-ui/` |
| Shared actions | One module both surfaces call, so they can never disagree about what an action does | `app/review_actions.py` |
| Schedule & infra | Hourly EventBridge Scheduler → one Lambda; CloudWatch + SNS alerting | `aws/template.yaml` |
| CI/CD | Tests on every push; AWS deploy via GitHub OIDC (no stored keys) | `.github/workflows/` |

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.14, FastAPI, Jinja2 + htmx |
| Data | PostgreSQL (Neon), SQLAlchemy 2, Pydantic 2 |
| Add-on | TypeScript, Google Apps Script (`CardService`), `esbuild`, `clasp`, Vitest |
| Cloud | AWS Lambda (arm64, SAM), EventBridge Scheduler, Secrets Manager, CloudWatch, SNS, IAM |
| CI/CD | GitHub Actions, GitHub OIDC (AWS deploy), `pytest` (real Postgres), Vitest |
| Local dev | Docker, `cloudflared` |

## APIs used

| API | For | Scope / model |
|---|---|---|
| Gmail API | Fetch, trash | `gmail.modify` (write used only for Trash) |
| Google Calendar API | Create/update/delete events | `calendar.events` |
| Google OAuth2 | Pipeline auth + the add-on's identity token | `gmail.modify`, `calendar.events`, `openid`, `.../userinfo.email` |
| Google Gemini API | Deadline/action-item extraction | `gemini-3.6-flash`, 20 requests/day free tier |

## Data model

Three tables (`app/db/models.py`): `processed_emails` (one row per email), `pipeline_runs` (one row
per run), `oauth_tokens` (the Google refresh token). No user table yet — single-user by design (see
[SCALING.md](../SCALING.md)).
