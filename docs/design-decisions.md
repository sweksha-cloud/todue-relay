# Design decisions

Why ToDue Relay is built the way it is: what was decided, what else was considered, and the
evidence behind it. The [README](../README.md) shows what the system does; this shows the
reasoning. Numbers are measured from this project's own runs and tests unless a source is named.

- [Correctness and reliability](#correctness-and-reliability)
- [LLM cost and safety](#llm-cost-and-safety)
- [Product behaviour](#product-behaviour)
- [Infrastructure](#infrastructure)
- [Scaling to other users (a plan)](#scaling-to-other-users-a-plan-not-built)
- [Known limitations](#known-limitations)

---

## Correctness and reliability

### 1. Postgres, not a stateless job
Each scheduled run starts on a fresh container with no memory. Without persistent storage every
run would re-send the same emails to the LLM and re-create the same calendar events. Postgres
gives idempotency, a crash-safe restart, and an audit trail of every extraction decision.
**Considered:** a state file (SQLite or JSON) committed back to the repo after each run. It avoids
hosting a database, but it makes git commits a side effect of a scheduled job, risks a race or
merge conflict if a manual trigger overlaps a scheduled run, and leaves the audit and correction
history to be hand-built on a flat file. Postgres also does natively what the claim logic needs,
an atomic conditional upsert (decision 2), and this was confirmed by testing it against a real
instance.

### 2. Atomic claims, and a short stale-claim window
Each email is claimed with a single Postgres `INSERT ... ON CONFLICT DO UPDATE ... WHERE ...
RETURNING`, so a crash or a re-run cannot double-create an event. A claim left behind by a
crashed run is reclaimed after `STALE_CLAIM_MINUTES`. The first value (15 minutes) was a
placeholder. A real run log showed one Gemini call takes about 1 second and per-email work is
dominated by a 13-second pacing delay, so the window is **3 minutes**: still 6 to 10 times the
realistic worst case for one email, and a stuck email is retried 5 times sooner.

### 3. A single-flight guard for overlapping runs
The daily Gemini budget counts *finished* runs only, so two overlapping runs would each think the
budget was untouched and double-spend it. The first idea, capping Lambda's reserved concurrency
at 1, is unavailable on a new AWS account (the account limit is 10). So the guard lives in the
database: a Postgres advisory **transaction** lock serializes the check-and-insert of a `RUNNING`
row, and a run exits at once if another started within `RUN_LOCK_TTL_MINUTES` and has not
finished. The expiry doubles as a lease for crashed runs, and because the lock is
transaction-scoped it is safe through Neon's connection pooler.
**Evidence:** a multi-connection race test (`backend/tests/test_run_guard.py`), and on AWS two
simultaneous real invocations produced one run, one skipped invocation and one database row.

### 4. Timezones are explicit
`CALENDAR_TIMEZONE` is required and never taken from the machine's clock (which is wrong on any
runner that is not the author's laptop), and offsets are attached to event times.
**Evidence:** regression tests for every bug found in real email (`EST`/`EDT`, spelled-out
regions, a `GMT+2` sign inversion) in `backend/tests/test_date_utils.py`.

### 5. Duplicate and changed deadlines
The same deadline arrives in several emails, and sometimes moves. Five sub-decisions, each made
with the alternatives written out (five ways to match were compared, including sender plus name,
embeddings and asking the LLM directly):
- **Match on event-name similarity alone** (a `difflib` ratio on normalized names, threshold 0.7,
  an untuned starting value). A Gmail thread ID is free and exact but only catches a reply in the
  same thread, and it misses the common case: an automated reminder system sending a brand-new,
  unthreaded email. Accepted risk: two unrelated deadlines with generic, similar names could be
  merged.
- **Always check the same day; search other days only when the email uses reschedule wording**
  ("postponed", "moved to", "new due date", and similar). The same-day check means a plain repeat
  is never missed; the keyword gate bounds how often the full-history scan runs. **Accepted gap:**
  a reschedule that states only a new date, with none of those words, creates a second event. It is
  a visible failure, not a silent one: two similar entries appear and the stray one is removed with
  the existing Remove action, in line with the project's rule of flagging uncertainty instead of
  hiding it.
- **No lookback limit.** A bounded window was built first, then reverted: at this scale (dozens of
  deadlines a year) comparing against every row is effectively instant, so accuracy wins over an
  optimization that is not needed.
- **A changed date updates the existing calendar event in place and is flagged on the
  dashboard**, so the calendar keeps one event per real deadline.
- **Compare exact date and time only when both sides have a time;** otherwise compare dates.

### 6. Skip real calendar invites
Rather than assume, the actual `.ics` part of every invite-shaped email in a real inbox was read.
Every one, whether from a person or a tool such as Zoom, was a well-formed `METHOD:REQUEST` with
the account as an `ATTENDEE`, which is what Gmail and Calendar already use to show an RSVP banner
or auto-add the event. Extracting the same email again would create a redundant second entry. So
any email carrying an invite part is skipped. A narrower rule ("only invites from a person") was
rejected because the data showed no real difference to draw.

### 7. True recurring events
When an email describes a repeating obligation ("rent is due on the 1st of every month"), the
pipeline creates a real recurring Calendar series instead of one one-off event for the next
occurrence.

---

## LLM cost and safety

### 8. A cheap pre-filter before any LLM call
A scoring pass over three independent signals (deadline keywords, action verbs, date-like
patterns) requires a minimum number of them before an email goes to the LLM. The default
`moderate` needs 2 of 3. **Evidence:** on a real 79-email inbox it sent about 39% to the LLM.
It is re-tunable against a real inbox with `backend/scripts/tune_filter.py`.

### 9. Confidence routing: an unsure model does not write to the calendar
Only a high-confidence deadline with a plausible date creates an event automatically. Everything
else goes to a review queue, and items with no fixed date (`needs_reply`, `unclear`) go to a
separate Action Items list. This matches the goal of not processing every item by hand, while
keeping uncertain ones in front of a person. Auto-created events can still be wrong, which is what
the Correct / Incorrect verdicts in decision 15 measure.

### 10. Implausible dates are flagged, never dropped
A date more than 3 days in the past or 365 days in the future is untrustworthy, but is never
auto-rejected: it is routed to review with a distinct `is_implausible_date` flag. The past bound
was loosened from 1 day to 3, because 1 day fired too often given the 2-day fetch window.

### 11. A 2-day fetch window
The Gmail query looks back `FETCH_WINDOW_DAYS=2`. It is easy to assume this should match the run
cadence, but idempotency (decision 2), not the window, is what prevents reprocessing. So the
window is sized to tolerate gaps between runs, not to avoid duplicates. This mattered: measured
schedule gaps on GitHub Actions reached 5.7 hours (decision 17), and the design absorbs that.

### 12. Protecting a 20-calls-a-day quota
The free Gemini tier allows 20 requests a day per project, resetting at midnight Pacific, and one
day's whole allowance was gone by early afternoon. Investigation showed 12 of that day's 15
calls came from development verification runs, because there was no way to check the pipeline
without running it for real. The response has four parts: usage is summed from finished runs in
`pipeline_runs`; a per-day budget stops a run once it is spent; a retry cap means 429 responses
do not count against an email; and a pacing delay spaces calls out.

### 13. Dry-run mode
`--dry-run` (and `{"dry_run": true}` for the Lambda) fetches, filters and reports what a real run
*would* do, spends no Gemini quota and writes nothing. The budget guard limits the damage of an
unneeded run; dry-run removes the reason to make one. It is how the AWS deployment was verified.

---

## Product behaviour

### 14. Server-rendered pages, not a single-page app
The dashboard is FastAPI + Jinja2 + htmx. React was considered and would be the more common skill,
but the requirements are a table, a few toggles, a summary and a status banner, which do not need
a separate build toolchain. A Gmail-embedded add-on was deferred as an additive layer for later.

### 15. Corrections are explicit; Remove and Reschedule are not votes
An auto-created event can be marked **Correct** or **Incorrect**. Removing or rescheduling it is
neutral: it may be wrong for reasons unrelated to extraction quality, so it does not count as an
"incorrect" vote, and a removed item leaves the list. The weekly correction rate is computed from
explicit votes only, so it measures extraction quality and not how often the user tidied the
calendar.

### 16. A metrics page
`/metrics` shows run history with per-run outcomes, the pre-filter pass rate (with an anomaly
flag), the weekly correction rate and Gemini usage against the daily quota, all computed from
data the pipeline already writes.

---

## Infrastructure

### 17. Scheduling moved from GitHub Actions cron to EventBridge Scheduler + Lambda
The Actions schedule said hourly, but scheduled workflows are best-effort. **Measured:** runs
every 2.5 to 5.7 hours, about 3.7 on average, over two days. On EventBridge, **16 of 16 scheduled
runs started exactly 60.0 minutes apart** (measured 2026-09-20 to 2026-09-21). The worst-case wait
to notice a deadline email fell from several hours to about one. Correctness never depended on it,
but it was not the cadence intended. Actions remains for CI and as a manual fallback trigger.

### 18. Failure containment
No automatic retries (a retry would spend scarce quota; the next hourly run is the retry), a
queued event is dropped after 60 seconds, and the overlap guard from decision 3 covers
concurrency. Two CloudWatch alarms email on any error and if the function has not run for about 3
hours, so a stopped schedule cannot go unnoticed.

### 19. Least-privilege IAM
Two roles. The function's execution role can write one log group and read one secret, and
nothing else. A separate scheduler role can only invoke that function, and only EventBridge
Scheduler in this account can assume it. The secret is named with IAM's single-character wildcard
(`-??????`) instead of `*`, which matches only that one secret.
**Evidence:** the policies were checked with IAM Access Analyzer before use, and the IAM policy
simulator confirmed the role is allowed on the real secret and implicitly denied on lookalike
names and on other services.

### 20. Where secrets and the OAuth token live
The Gemini key and database URL are in AWS Secrets Manager, with per-secret access control and an
audit trail. The Google OAuth token deliberately lives in Postgres instead: it is refreshed by a
re-consent done on a laptop that already has the database connection, so Secrets Manager would
need a second write path and an AWS identity for that step. Both directions of the trade-off were
weighed. The token also cannot live on a local disk, because CI runners and Lambda have none.

### 21. Infrastructure as code with AWS SAM
The whole AWS setup (function, log group, schedule, both roles, alarms, alert topic) is one SAM
template, `aws/template.yaml`. The setup was first built by hand to learn each piece; the
hand-built runbook then drifted from what was deployed (different memory, timeout and scheduler
type), which is exactly what a file in git prevents. SAM fits a single Lambda plus a schedule with
no state file to protect. Terraform was considered and would be more portable, but the AWS part
of this project is time-boxed, so portability buys little; CDK needs Node and a bootstrap stack.
**Cutover without downtime:** the template was deployed as a separate stack with its schedule
disabled, compared field by field with the running function (identical), dry-run invoked, and
only then switched over. A plain redeploy against the live stack now reports no changes. The
secret is referenced by name and never appears in the template.

### 22. Security review
A dedicated pass, with every finding fixed and verified or explicitly accepted:
- **Dependencies:** `pip-audit` found real CVEs in five packages; all were upgraded and the suite
  re-run.
- **CI permissions:** both workflows now run with `contents: read`, since neither touches the
  GitHub API.
- **XSS from LLM output:** rows seeded with script payloads in every LLM-controlled field rendered
  HTML-escaped, tested directly rather than assumed.
- **Injection:** no raw SQL, `os.system`, `subprocess`, `eval` or `exec`; all access goes through
  SQLAlchemy's parameterized queries.
- **Secrets:** every commit scanned; only the empty `.env.example` placeholder matched.
- **Accepted, with reasoning:** the dashboard has no authentication, which is why it runs locally
  and must not be hosted until it has some.

---

## Scaling to other users (a plan; not built)

Today the system is single-user by design: one Google account, one calendar, one OAuth token, one
Gemini key, configuration in environment variables. This is how it would grow to serve other
people, in the order I would do it. It is a plan, not a claim about what exists.

### Two constraints that engineering cannot remove

1. **Google's restricted scope.** Reading mail uses `gmail.readonly`, a *restricted* scope. An app
   that reads it and stores the data on a server must pass Google's OAuth verification and an
   annual third-party security assessment (CASA), which costs from a few hundred to a few
   thousand dollars a year. Until then an unverified app is capped at 100 users, shows an
   "unverified app" warning, and while in Testing status its refresh tokens expire every 7 days.
   Sign-in itself is different: `openid`, `email` and `profile` are non-sensitive and need no
   verification at any scale.
2. **The LLM's free tier is shared.** Gemini's free tier allows 20 requests a day *per project*,
   so a second user would halve the first user's budget. Serving others needs a paid pool with
   per-user limits, or each user bringing their own key.

### What would change, in dependency order

1. **Identity.** "Sign in with Google" (identity scopes only), server-side sessions in HttpOnly,
   SameSite cookies, and CSRF protection on every state-changing endpoint (Remove, Reschedule,
   Schedule), since those buttons write to a real calendar.
2. **A tenant-aware data model.** A `users` table and a `user_id` on `processed_emails`,
   `pipeline_runs`, corrections and OAuth tokens. The atomic claim key becomes `(user, email)`.
   Every query goes through a repository function that *requires* a user, so there is no way to
   write a global query by accident; Postgres row-level security would be a second layer. Tests
   must prove isolation, including guessing another user's row IDs in a URL.
3. **Real migrations.** Schema changes are manual `ALTER TABLE` today, which is fine for one
   person. With many users they need versioned, reversible migrations (Alembic), and existing rows
   backfilled to the owner.
4. **Per-user credentials.** OAuth refresh tokens and any bring-your-own Gemini keys encrypted at
   rest with envelope encryption (a KMS-managed key), never logged, with a revoke-and-delete flow.
   Expired or revoked consent becomes a normal state: an in-app "reconnect" banner and an email,
   not a failed run that only the owner sees.
5. **Pipeline fan-out.** Today one scheduled Lambda processes one mailbox. A dispatcher would
   enqueue one job per active user (SQS), a worker would process one user's mailbox, and failures
   would be isolated per user with a dead-letter queue. The existing single-flight guard
   generalizes by keying the advisory lock on the user; a global concurrency cap protects the LLM's
   rate limits.
6. **Quota and cost control.** A per-user daily budget, a shared rate limiter across workers,
   usage metering per user, a kill switch, and a cost alarm. The budget guard that exists now
   counts one user's finished runs; it becomes a per-user counter plus a global one.
7. **Privacy.** Store as little as possible (subjects and extracted fields, not full bodies), a
   retention period with automatic deletion, an export-and-delete-my-account endpoint that also
   revokes the Google grant, no email content in logs, a published privacy policy, and compliance
   with Google's Limited Use rules.
8. **Per-user settings.** `CALENDAR_TIMEZONE`, filter level and fetch window move from environment
   variables to a settings row per user; an onboarding flow connects Google and picks a calendar.
9. **API and front end.** A versioned JSON API with a generated typed client, and a separate
   front end (React and TypeScript) on a static host, replacing the server-rendered pages.
10. **Security hardening.** Rate limiting on the API, a permission boundary on the CI deploy role,
    dependency scanning in CI, and a review of every endpoint for authorization, not only
    authentication.
11. **Operations.** Structured logs keyed by a hashed user ID, aggregate alerts (share of users
    whose last run failed), a staging environment, and a load test with fake mailboxes.
12. **Infrastructure limits.** Neon's free tier caps connections, storage and compute; a new AWS
    account's Lambda concurrency limit is 10 (raisable on request); and the AWS Free plan ends in
    March 2027, so a real service needs a paid account or another host.

### What would stay the same
The atomic per-email claim, confidence routing, the dry-run mode, the single-flight guard (now per
user), the pre-filter, the alarms, and infrastructure as code with SAM. The idempotency and
safety design was built around one person's mailbox, but nothing in it assumes there is only one.

### A rollout that fits a zero-dollar budget
1. **A public demo with sample data**, no Google connection, so anyone can try the interface.
2. **An invite-only beta** of allow-listed testers (up to 100, in Google's Testing status, each
   re-consenting weekly), which is enough to exercise multi-tenancy for real.
3. **Self-hosting instructions**, so anyone can run it with their own Google project and keys.

General availability needs Google's verification and security assessment, which is a cost decision
and not an engineering one.

---

## Known limitations

- **Weekly Google re-authorization.** While the OAuth app is in Google's "Testing" status, its
  refresh token expires every 7 days. The pipeline fails with a clear error, an alarm emails, and
  `python -m scripts.reauth_google` fixes it in about a minute; a weekly reminder prevents it.
  Publishing the app would remove the expiry but needs a privacy policy and branding page.
- **The AWS deployment is time-boxed.** It runs on the AWS Free plan's credits, ending March
  2027, with the plan to move the schedule back to GitHub Actions. That brings the best-effort
  cadence back; the pipeline is built to tolerate it (decisions 2 and 11).
- **The dashboard is local and unauthenticated by design** (decision 22).
- **Single user.** One Google account, one calendar. Serving others is planned above, and is
  limited chiefly by Google's verification rules and the LLM's free tier.
