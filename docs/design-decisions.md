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

### 6. Skip anything Google Calendar already handled
Rather than assume, the actual `.ics` part of every invite-shaped email in a real inbox was read.
Every one, whether from a person or a tool such as Zoom, was a well-formed `METHOD:REQUEST` with
the account as an `ATTENDEE`, which is what Gmail and Calendar already use to show an RSVP banner
or auto-add the event. Extracting the same email again would create a redundant second entry. So
any email carrying an invite part is skipped. A narrower rule ("only invites from a person") was
rejected because the data showed no real difference to draw.
**Amendment: an `.ics` attachment alone missed real cases.** Two real bugs surfaced from it: a
"New event" notification (an event added to a shared calendar, no `.ics` attached) was extracted
and given a second, duplicate Calendar event; a "Canceled event" notification (nothing real to
act on) was extracted as an "unclear" action item. The first fix checked for Google's
`X-Google-Calendar-Notification` header; checked against five real, distinct patterns
("New event:", "Canceled event:", a real "Invitation:", "Updated invitation:", "Accepted:"), that
header was present on only two. All five instead carry the same `Sender: Google Calendar
<calendar-notification@google.com>` — the actual RFC 5322 sending agent, kept distinct from
`From` (the human organizer) so a reply goes to the right place — so the check now matches on
that Sender address instead, alongside the original `.ics` check for third-party tools that never
go through Google Calendar's own relay at all.

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

### 23. Automated deploys through GitHub OIDC, with no stored AWS keys
A push to `main` that touches the Lambda's code or template runs the tests and then deploys the SAM
stack: build the arm64 package on a native runner, assume an AWS role, `sam deploy`, then dry-run
the function and fail the run unless it returns cleanly. GitHub proves its identity with a
short-lived OIDC token, so no AWS access key exists anywhere to leak. The AWS side trusts only this
repository on `main`, matched on GitHub's immutable subject format (the older `repo:owner/name`
form would silently fail on a repository this new).
**Least privilege, in two roles.** The role GitHub can assume may only ask CloudFormation to update
one stack, upload to SAM's bucket, invoke the function for the smoke test, and hand the second role
to CloudFormation. It cannot create a Lambda, a role or a schedule itself. The second role is what
CloudFormation uses while applying the stack, limited to the named resources. Both live in a
separate stack (`aws/ci-template.yaml`) applied by hand, so the role GitHub assumes is not something
a deploy can rewrite. Both policies pass IAM Access Analyzer with no findings.
**Evidence, including the miss.** The first run authenticated and uploaded the package, then failed:
the SAM transform runs under the CloudFormation role's own credentials, and only the GitHub role had
been given permission to use it. AWS's error named the missing permission exactly; the fix was one
statement, and the second run succeeded (tests 42 s, deploy 1 m 37 s, dry-run smoke test returning
HTTP 200). Rolling back is `git revert` and a push.

### 24. A Gmail add-on that holds no decisions
A second screen for daily use: a side-panel card in Gmail showing what needs review, what is coming
up and the action items. The htmx dashboard stays; this reads the same data.
**Why the API is its own app.** An add-on runs in Apps Script on Google's servers, so it cannot call
a laptop's `localhost`; the API needs a public HTTPS address. The dashboard has no login, so the
add-on's routes are a separate tiny app (`addon_app.py`) that contains nothing else, with the
interactive docs off. Giving it a public address cannot expose the dashboard.
**Who may call it.** Each request carries the Google identity token of the signed-in user. The API
verifies the signature, expiry, issuer and audience (a token minted for a different app is rejected),
requires a Google-verified email, and allows exactly one address. With either setting empty it refuses
everything. Rejections reveal nothing about why.
**Where the logic lives.** Apps Script can only be seen in Gmail and is awkward to debug, so the
add-on decides nothing. What counts as "needs review" or "upcoming", how a deadline is worded, and even
which buttons an item offers live in the API in tested Python. The four review actions (vote, remove,
approve, decline) moved out of the dashboard's routes into one shared module, so the dashboard and the
add-on cannot disagree about what an action does; `/approve` and `/decline` had no tests before that
move, so they got 20, checked against the pre-move code. The add-on's own logic is plain TypeScript with Google's services
injected, and only the widget layout is left to a manual checklist.
**Evidence.** Backend tests use real RS256 tokens verified against a fake key endpoint, and check that every
write route refuses a missing or someone else's Google token; 113 add-on tests cover the logic, every way the
API call can fail, the card layout against a recording fake of Google's card builder, and the real bundle
run in a sandbox that stands in for Apps Script. A snapshot of the API's response shapes is checked from both
sides, so neither can change without the other. Finally the real bundle was run over real HTTP against the
real API on sample data, with only Google's key endpoint and Calendar faked: 17 checks, including that a
different Google account is refused and cannot remove an event. Deliberately
breaking the audience, email and verified-email checks, and separately the login header, the "more"
note and a global entry point, was each caught by the test meant for it. A bundle-level test earned its
place by covering what module tests cannot: Apps Script has no module system, so the entry points must
be attached to the global object.
**Alternatives.** A hosted React front end gives more front-end signal and is planned separately;
a browser extension works in one browser and cannot run in the background; an Apps Script-only version
would mean rewriting the pipeline in JavaScript.
**Status, stated plainly:** written and tested, not yet installed in a real Gmail, so the manifest and
layout are unproven against Google until the first install.


### 25. A Gemini outage does not use up an email's retries
**The incident.** While checking the runs after a deploy, three good emails were found permanently
`failed`: each had used its 3 attempts on Gemini's "503 UNAVAILABLE, high demand", a temporary capacity
error on Google's side, and been parked for good. The design already exempted 429 rate-limit errors from
the retry cap, but only 429; any other error, a 5xx included, counted as if the email were to blame.
**What the cap is for.** It stops a poison email (one that always fails) from calling Gemini every hour
forever. That is worth keeping, and the fix must not undo it.
**The fix.** A 5xx from Gemini and a dropped connection are now transient like a 429: the attempt is
refunded, so the email stays retryable through an outage. Anything about the request itself (a 400, a bad
response, an unparseable date) still counts.
**The bound, which is the important part.** Refunding without limit would be a bug of its own: the
recovery sweep re-fetches failed emails by id whatever the 2-day fetch window, so an email that triggers a
*permanent* server error would be retried every hour indefinitely. So the refund applies only while the
email is younger than `TRANSIENT_RETRY_WINDOW_HOURS` (default 48, matching the fetch window). Past that a
failure counts normally and the cap takes over, which bounds the cost of a misclassified email to about two
days of hourly attempts.
**Alternatives.** In-run retries with backoff spend extra calls against a 5-per-minute, 20-per-day limit and
the next hourly run is already a retry. Raising the cap only delays parking a poison email and does not cover
a long outage. A separate transient-failure counter is more precise but needs a manual `ALTER TABLE` on the
production database, and the age bound needs none.
**Recovery for the emails already parked.** `python -m scripts.retry_failed` lists (and with `--apply`
gives fresh attempts to) rows that failed *only* with a transient error, leaving emails that failed for
their own reasons parked. It changes nothing without `--apply`.
**Evidence.** 29 new tests. Deliberately removing the age bound, un-parking rows that failed for their own
reasons, reverting the pipeline to refund only 429s, and dropping the 5xx translation were each caught by
the test meant for it.


### 26. Tell a person when an email fails for good
**The gap.** A run reports SUCCESS whenever the run itself completes, however many emails failed inside it.
Three good emails sat parked for days, with 2 failures per hourly run, before anyone noticed: the existing
alarms watch for a function error or a schedule that stopped, and neither fires when the function runs fine
and quietly gives up on individual emails.
**The fix.** When an email uses its last attempt, the run sends one email through the alert topic the
CloudWatch alarms already use, so there is nothing new to subscribe to. It lists the emails and the error,
and says how to retry a temporary one. The dashboard also shows a banner for anything currently parked, so the
state is visible even if the email was missed. One alert per run however many emails were parked, and none
for an email that fails but has attempts left (that is retried, and a transient error never parks it).
**Why "parked" is the trigger.** Alerting on every failure would email through every Gemini blip, which is
exactly what decision 25 stopped counting. Parking is the rare, permanent, actionable event. It is detected
where it happens: a failed email is only claimed again while it has attempts left, so one that is out of
attempts right after failing has just been parked, and cannot alert twice.
**It cannot hurt a run.** Sending is best-effort: no topic configured (a local run, GitHub Actions) only logs;
an SNS error is swallowed and logged; and the pipeline wraps the call as well, so a notification problem can
never turn a good run into a failed one.
**Least privilege.** The function's role gains `sns:Publish` on that one topic and nothing else; the topic
address reaches it as an environment variable set by the template.
**Checking it end to end.** Invoking the function with `{"test_alert": true}` sends a test alert and runs
nothing else, which proves the role may publish and the email arrives.
**Evidence.** 31 new tests. Deliberately alerting on every failure, letting an alert error fail the run,
counting an email as parked while it has attempts left, and sending an unsanitised subject were each caught by
the test meant for it. One further change (alerting in a dry run) cannot be caught, and does not need to be:
a dry run never claims an email, so nothing can fail there; the guard is defence in depth.
**Alternatives.** A CloudWatch metric filter and alarm on a log line avoids touching the function's role but
adds two more resources (and permissions for the deploy role) for a rare event; an alarm on the run's
failed-email count would fire on every transient blip, the noise decision 25 removed.


### 27. The dashboard groups emails by the decision made about them
**The problem.** One long table mixed everything: emails waiting on a decision, ones already checked and
right, ones that failed. Nothing separated "needs me" from "done", so the useful rows were buried.
**The groups.** Needs your review; on your calendar to check; marked incorrect; failed; marked correct;
denied or skipped (and a rare in progress). The ones wanting a decision start open, the finished ones
collapsed, each with a count. Action items keep their own panel and removed items stay hidden.
**What an item needing review offers.** Approve (add it at the date that was found), Reschedule (add it at a
time you choose, which is also the way to add one whose date is wrong or missing, since Approve needs a date), and
Deny. Approve is only offered when there is a date to approve.
**A partition, checked.** Every email the dashboard lists is in exactly one group. That is enforced by one
definition (`app/categories.py`) with its queries beside the others, and a test that builds every state and
checks nothing is lost or listed twice, so the groups cannot quietly disagree with the list they divide.
**Why not tabs.** A first version filtered with tabs; it hid the sections behind clicks and was rejected.
Stacked sections show everything at once and let the finished ones fold away. Each section pages on its own
without resetting the others.
**Why a button reloads the page.** An email that changes decision must move to another section, so
swapping only its row (the old behaviour) would leave it in the wrong place. The button now asks the browser
to reload, which keeps the query string and so the pages being viewed.
**Reuse.** The Gmail add-on can show the same groups from the same definition, and already gets its buttons
per item from the API.
**Evidence.** 54 new tests (383 total), including the partition property; five deliberate breakages (a group
swallowing another's emails, a button not reloading, paging one section resetting the rest, and more) were each
caught by the test meant for it.


### 28. A "move to trash" button, and widening Gmail access on purpose
**The ask.** A button next to every row to get rid of the source email itself, not just hide it
from the dashboard or remove its Calendar event — those two already existed (Dismiss, Remove) and
neither touches Gmail at all.
**The scope trade-off, stated plainly.** Gmail access was `gmail.readonly` (decision 19): the
scheduled pipeline never writes to Gmail, and that was a real selling point. Trash needs
`gmail.modify`, a real widening. The choice made: Trash only, never a permanent, bypass-Trash
delete — the same as clicking Gmail's own trash icon, recoverable there for 30 days (Gmail's own
default, not something this project controls). The **scheduled pipeline still only ever reads**;
the new capability is used from exactly one place, an explicit, user-initiated dashboard click.
**Deliberately orthogonal to the other two actions.** Trashing the email does not touch an
existing Calendar event, a recorded vote, or the extraction's status beyond hiding the row — "get
rid of the email" and "get rid of the calendar event" are different intents, so they stay two
separate buttons (Trash and Remove) that can be used together or alone.
**How it hides the row.** Reuses the exact mechanism Remove already established:
`not_removed()`'s message check, which every listing query (the category sections *and* Action
Items) already filters through — one new message, `TRASHED_MESSAGE`, added to the same set. No
new column, no per-list-type special case.
**Idempotent, the same reasoning as decision-25's era Calendar delete fix:** trashing an
already-trashed (or already-gone) message returns 404 from Gmail; treated as success, since the
goal ("not in the inbox") is already true.
**Cost.** Every user of the app must re-run the OAuth consent flow once (`reauth_google`) before
this button works, since a token issued under the old, narrower scope keeps refreshing under that
same narrower grant — only a fresh consent grants a new scope.


---

## Scaling to other users (a plan; not built)

ToDue Relay is single-user by design. How I'd extend it to other people is written up in
[SCALING.md](../SCALING.md) as a design exercise, not a pending roadmap item. The short version:
two constraints shape everything (Google requires a paid annual security assessment before an app
that reads Gmail can open to the public, and the LLM's free tier is shared per project), and the
work is a per-user data model with isolation tests, separate sign-in and Gmail consent, per-user
fan-out through a queue, per-user quota and encrypted credentials, and a privacy and retention
policy. The safety design (atomic claims, confidence routing, dry-run, the single-flight guard) stays
and gains a user dimension.

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
