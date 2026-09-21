# Scaling ToDue Relay to multiple users

> ToDue Relay is a deliberately personal-use tool. This document is a design exercise in how I'd
> extend it to other people, not a pending roadmap item, and nothing in it is built. The current
> design and its evidence are in [README.md](README.md) and
> [docs/design-decisions.md](docs/design-decisions.md).

## Where it starts

One Google account and one OAuth token, one Gemini key, one calendar, configuration in environment
variables, one hourly Lambda that processes one mailbox, and a Postgres schema with no notion of a
user. The dashboard runs locally with no authentication. The per-email safety design (atomic claims,
confidence routing, dry-run, a single-flight guard, quota protection) assumes one person's mailbox,
but nothing in it assumes there is only one.

## Two constraints engineering cannot remove

1. **Google's restricted scope.** Reading mail needs `gmail.readonly`, a *restricted* scope. An app
   that reads it and keeps the data on a server must pass Google's OAuth verification and an annual
   third-party security assessment ([Google's rules](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)),
   which costs [from a few hundred to a few thousand dollars a year](https://deepstrike.io/blog/google-casa-security-assessment-2025).
   Until then an unverified app is capped at 100 users and shows an "unverified app" warning, and
   while in Testing status refresh tokens expire every 7 days. Signing in is different: `openid`,
   `email` and `profile` are non-sensitive and need no verification at any scale.
2. **The LLM's free tier is shared.** Gemini's free tier allows 20 requests a day per project, so a
   second user would halve the first user's budget.

Everything below is shaped by those two facts.

---

## 1. How each user connects Google

| Option | How it works | For | Against |
|---|---|---|---|
| **A. One shared OAuth client** | Users click "Connect Gmail" on a hosted service | Best experience | Needs Google's verification and the paid annual assessment before it can open to the public |
| **B. Bring your own Google project** (BYOK-style) | Each user creates a Google Cloud project and OAuth client and pastes the credentials in | No central verification: each user's own project only ever serves them | Heavy onboarding, and each user still has to publish their own app to avoid weekly re-consent |
| **C. Browser extension** | Processing runs in the user's browser under their own session | No mailbox stored on my servers | Works in one browser on one machine, does nothing when the browser is closed (no background schedule), and it still needs an OAuth client. I have not verified how Google's assessment rules treat a client-only design, so I would treat that as an open question |
| **D. Self-hosted** | Each person runs their own copy with a `docker compose` file | Free, private, no verification | Only technical users |

**What I'd do:** D first, because it costs nothing and is honest about the constraint. Then an
invite-only beta on option A while the app is in Google's Testing status (up to 100 allow-listed
testers who re-consent weekly), which is enough to exercise multi-tenancy for real. Option A for the
public only if there were demand and a budget for the assessment.

## 2. LLM cost model

On this instance the pre-filter sends about 39% of a real inbox to the LLM, and a typical day makes a
handful of calls (3 of the 20 available on the day it was checked). Real usage per user is small, so
the risk is not cost per user, it is the shared free quota and abuse.

| Model | For | Against |
|---|---|---|
| **Platform key with metering** | Zero friction | The free tier cannot serve more than one user; a paid key means I carry the cost and the abuse risk |
| **Bring your own Gemini key** | Cost and quota are the user's | Onboarding friction, and I would hold users' keys, which need encryption at rest and careful handling |
| **Hybrid (what I'd pick)** | A small capped free allowance on the platform key, and a user's own key lifts the cap | Two code paths to test |

Metering either way: a per-user daily counter in Postgres (the current budget guard already counts
one user's finished runs; it becomes per-user plus a global counter), a shared token-bucket rate
limiter across workers, a global kill switch, and a cost alarm. Before handling anyone else's mail I
would also read the current terms for the Gemini API, because how submitted content may be used can
differ between free and paid tiers, and that belongs in the privacy policy (section 6).

## 3. Database changes

- A `users` table, and a `user_id` (indexed) on `processed_emails`, `pipeline_runs`, corrections and
  OAuth tokens. The atomic claim key becomes `(user, email)`, so two users receiving the same
  newsletter never collide.
- Every query goes through a repository function that **requires** a user, so a global query cannot
  be written by accident. Postgres row-level security would be a second layer.
- Tests that prove isolation, including one user guessing another's row IDs in a URL (an insecure
  direct object reference).
- Per-user settings (timezone, filter level, fetch window) move from environment variables to a
  settings row.
- OAuth tokens and any user-supplied keys in their own table, encrypted at rest.
- **Real migrations.** Schema changes are manual `ALTER TABLE` today. With many users they need
  versioned, reversible migrations (Alembic), with existing rows backfilled to the owner, and a
  migration step that runs before the new code deploys.

## 4. Accounts and sessions for the dashboard

Signing in and connecting Gmail are two different consents, and I'd keep them separate:

- **Sign in with Google**, identity scopes only (no verification needed). That establishes who the
  user is. It must not request mail access.
- **Connect Google**, a second step that requests Gmail and Calendar access using incremental
  authorization, so a user can sign in, look around a demo, and decide later.
- **Sessions:** server-side sessions in HttpOnly, SameSite cookies; CSRF protection on every
  state-changing endpoint (Remove, Reschedule and Schedule write to a real calendar); logout that
  invalidates the session; rate limiting on the API.
- **Authorization, not only authentication:** every endpoint checks that the row belongs to the
  signed-in user.
- **Account deletion** that removes the user's rows and revokes their Google grant.

## 5. Deployment: Lambda, EventBridge and the SAM template

Today one hourly schedule starts one Lambda that processes one mailbox, and the single-flight guard is
one global lock. At real multi-user volume:

- **Fan-out.** The hourly schedule starts a small *dispatcher* Lambda that finds users due for a run
  and puts one message per user on an SQS queue. A *worker* Lambda processes one user per message, so
  one user's failure never blocks the others, with a dead-letter queue for repeated failures.
- **The guard becomes per-user.** The advisory lock is keyed on the user id, so two runs for the
  same user still cannot overlap, and different users run in parallel.
- **Smoothing.** A per-user `next_run_at` staggers work across the hour instead of stampeding at :00.
- **Concurrency.** The queue's worker concurrency is capped to protect downstream limits. A new AWS
  account's Lambda concurrency limit is 10 (raisable on request), which would be the first ceiling.
- **Rate limits across APIs.** Gmail and Calendar have per-project and per-user quotas, and Gemini has
  per-minute and per-day limits per project; the current numbers should be checked before sizing. A
  shared token bucket plus exponential backoff with jitter on 429 and 503 responses, on top of the
  retry cap that exists today. Retries are safe because claims are idempotent.
- **Timeouts.** A Lambda run is capped at 15 minutes, so each user's job must fit or resume.
- **Database connections.** Many concurrent workers exhaust a small Postgres: use the pooled
  connection string (already needed for the advisory locks to behave) and cap worker concurrency to
  the pool.

**What changes in the SAM template (`aws/template.yaml`):**

- New resources: the dispatcher function, the worker function with a concurrency cap, an SQS queue
  and its dead-letter queue, and a KMS key for encrypting stored credentials.
- New alarms: queue age, dead-letter depth, and the share of users whose last run failed, alongside
  the existing errors and not-running alarms.
- A parameter per environment, so a **staging stack** is the same template with a different name
  prefix and its schedule disabled. The template is already parameterized this way.
- The deploy pipeline (`aws/ci-template.yaml`, the GitHub OIDC roles) needs permissions for the new
  resources, still scoped by name, and a step that applies database migrations before the deploy.
- The AWS Free plan ends in March 2027, so a real service would need a paid account or another host.

## 6. Privacy and compliance

Before handling anyone else's email I'd want, at minimum:

- A **privacy policy** that names the scopes, what is stored, how long, who processes it (including
  the LLM provider as a subprocessor), and how to delete it. Google requires it for verification.
- An explicit, granular **consent flow** that shows exactly what is read and what is stored, and can
  be revoked at any time.
- **Google's Limited Use requirements** for data obtained through restricted scopes: use it only to
  provide the user-facing feature, never for advertising, never sold.
- **Data minimization:** store subjects and extracted fields, not full message bodies, and never write
  email content to logs.
- A **retention period** with automatic deletion, and an **export and delete-my-account** path that
  also revokes the Google grant.
- **Encryption** in transit and at rest (a KMS-managed key for tokens and keys), and no standing
  human access to user data, with audit logging when there is any.
- A **breach response plan.**
- **Legal review** for GDPR and CCPA-type obligations if users in those places sign up. I'm not a
  lawyer and would not rely on this list for that.

---

## What stays the same

The atomic per-email claim, confidence routing, the pre-filter, dry-run mode, the alarms, and
infrastructure as code with SAM. The safety design does not change, it gets a user dimension.

## A rollout that fits a zero-dollar budget

1. **Self-hosting instructions** (option D).
2. **An invite-only beta** of allow-listed testers (option A, Testing status).
3. **A public demo** with sample data and no Google connection, so anyone can try the interface.

Open to the public with real Gmail access needs Google's verification and security assessment, which
is a cost decision, not an engineering one.
