# Postmortem: hourly runs failed after the Google sign-in expired (Nov 2026)

Blameless format: what happened, why, and what changed. Exact run-by-run times are in the
CloudWatch alarm history for `todue-relay-sam-pipeline-errors` and the Lambda logs.

## Summary

- **What broke:** every hourly run of the pipeline failed. No emails were read and no calendar
  events were created.
- **Why:** the Google OAuth app is in "Testing" status, where Google expires refresh tokens 7 days
  after sign-in. The token expired, so every run stopped at the first Google API call.
- **Impact:** deadlines arriving during the outage weren't scheduled until the next successful
  run. No data was lost: the pipeline tracks each email in Postgres, and its 2-day Gmail window
  picked up the missed emails once runs succeeded again.
- **Detection:** the CloudWatch error alarm emailed through SNS.
- **Resolution:** signed in to Google again (`python -m scripts.reauth_google`); the new token is
  stored in Postgres, so the next scheduled run used it without a redeploy.

## Timeline (Nov 2026)

1. **7 days after the previous sign-in:** Google expires the refresh token. The next hourly run
   fails with `invalid_grant`, which the pipeline raises as `GoogleReauthRequired` (with the
   re-sign-in command in the message).
2. **Same hour:** the CloudWatch alarm on Lambda `Errors` goes to ALARM and emails.
3. **Following hours:** every run fails the same way. The alarm stays in ALARM.
4. **Nov 21:** the owner signs in again; the token is saved to the database.
5. **Next run:** succeeds; the alarm returns to OK and emails once more.

## Root cause

A known limit, documented in the README, that depended on a person remembering a weekly
step. Google's 7-day expiry applies only to apps in "Testing" status. Keeping the app there
avoided a verification review, but turned a one-time setup into a recurring manual task with
no reminder that actually fired.

## What went well

- **The failure was loud and specific.** The error names the cause and the fix, and the alarm
  emailed instead of the pipeline failing silently.
- **Nothing was lost or duplicated.** Per-email tracking in Postgres (idempotent runs) and the
  2-day fetch window let the pipeline catch up on its own.
- **Recovery needed no deploy.** Because the token lives in the database, not the Lambda
  package, re-signing in fixed production directly.

## What went badly

- **The fix depended on memory.** The weekly sign-in had no working reminder.
- **The alarm was noisy by design.** It fired on every single error, so one failed run and a
  real outage looked the same.

## Action items

| Action | Status |
|---|---|
| Replace the any-error alarm with SLO burn-rate alarms (95% of runs succeed over 7 days; page when 3 of the last 6 runs fail), so one blip doesn't page and an outage does ([decision 29](../design-decisions.md)) | Done, Nov 22 |
| Publish the Google OAuth app to "In production", which removes the 7-day expiry entirely (still unverified, single user) | Open |
| Until then, a recurring calendar reminder to sign in before day 7 | Open |
