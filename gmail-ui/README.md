# ToDue Relay Gmail add-on

A home card in Gmail's side panel: what needs review, what's coming up on the calendar, your action
items, and how the last run went, with buttons to add, reschedule or decline a held-back item,
schedule an action item, mark an event correct or incorrect, reschedule or remove it. It is a second
screen over the same data and the same actions as the htmx dashboard, which is unchanged.

**Status:** the code and its tests are done, and it has been run end to end (below) and installed in a
real Gmail. Not built yet: a card for the open email.

## How it fits together

```
Gmail  →  add-on (Apps Script, runs on Google's servers)
              │  fetches with the Google identity token of whoever is signed in
              ▼
          add-on API  (backend/app/addon_app.py)   ← verifies the token, allows ONE email
              ▼
          Postgres (the same database the pipeline writes)
```

The add-on runs on Google's servers, not in your browser, so it cannot call `localhost`: the API needs
a public HTTPS address (a tunnel while developing). The API is its **own small app**, not the
dashboard, so giving it a public address exposes nothing else.

## Where the logic lives, and how each part is tested

The principle: keep decisions out of Apps Script, because it can only be seen in Gmail. Everything that
decides something is in tested code; the untested surface is only the layout of widgets.

| Part | Where | Tested by |
|---|---|---|
| What counts as "needs review" / "upcoming", how a deadline is worded, who is allowed in | the API, `backend/` | `pytest` (`test_addon_api.py`, `test_addon_auth.py`) |
| How the card reads (titles, tags, "+ N more", empty states, error hints) | `src/format.ts` | `vitest` unit tests |
| Calling the API and every way it can fail | `src/api.ts` | `vitest`, with fake Google services injected |
| Turning that into Google's widgets | `src/cards.ts` | `vitest`, against a recording fake of `CardService` |
| The bundled script exposes `onHomepage`, `onAction` etc. as globals and wires the pieces together | `dist/Code.js` | `vitest`, running the real bundle in a sandbox |
| The add-on and the API agree on the response shapes | `contract/api.schema.json` | a backend test fails if the API's models drift from the snapshot; the add-on's tests check their fixtures against the same file |
| How it *looks* in Gmail | | **manual** checklist below |

```
npm install
npm run typecheck && npm test      # 113 tests
npm run build                      # writes dist/Code.js and dist/appsscript.json
```

## What has been run end to end

Beyond the unit tests, the real built bundle was run against the real API, over real HTTP, backed by the
fake-data database, with only Google's token-key endpoint and Calendar faked. Seventeen checks passed:
the card shows the sample items, approve / vote / remove go through and the card reflects them (a voted item
loses its vote buttons), a refused action shows the API's own reason and leaves the card alone, and a
different Google account is refused, sees no data, and cannot remove an event. The harness was a scratch
script and is not part of the repository; the unit and bundle tests are.

## First install (about 20 minutes)

1. **Create the Apps Script project.** At <https://script.google.com> choose New project, name it
   "ToDue Relay", then Project Settings and copy the **Script ID**. Also enable the Apps Script API at
   <https://script.google.com/home/usersettings>.
2. **Point `clasp` at it.** `cp .clasp.json.example .clasp.json` and paste the Script ID. Then
   `npx @google/clasp login`. (`.clasp.json` is gitignored.)
3. **Push.** `npm run push` uploads `dist/` (the bundle and the manifest).
4. **Find the client id the API must expect.** In the Apps Script editor open `Code`, choose the
   function `debugToken`, and Run it. Approve the permissions (Google warns the app is unverified: that is
   expected for your own script; choose Advanced, then Go to ToDue Relay). The execution log prints the
   `aud` and your email.
5. **Start the API and a public tunnel, against the fake-data database, never the real one.** From the
   repo root:
   ```
   PREVIEW_DATABASE_URL=<the fake-data database> \
   ADDON_OAUTH_CLIENT_ID=<the aud from step 4> \
   ADDON_ALLOWED_EMAIL=<your email> \
   gmail-ui/scripts/dev.sh
   ```
   It refuses a database whose name does not contain "preview", starts the add-on API (its own app: the
   dashboard is not served) and a `cloudflared` tunnel (`brew install cloudflared`), and prints
   `API_BASE_URL = https://….trycloudflare.com`. Ctrl-C stops both. The address changes each time it restarts.
6. **Tell the add-on where the API is.** Project Settings, Script properties, add `API_BASE_URL` with the
   address the script printed (no trailing path).
7. **Install it in your Gmail.** In the editor choose Deploy, then Test deployments, then Install
   (the menu wording has changed over the years; look for the Gmail add-on test install). Reload Gmail:
   the ToDue Relay icon appears in the right-hand panel.

Each later change: `npm run push`, reload Gmail.

## Manual checklist (after meaningful changes)

- [ ] The home card opens and the header shows the last run.
- [ ] All three sections show, with sensible titles and counts.
- [ ] An empty list says so; more than ten items shows "+ N more in the dashboard".
- [ ] Low-confidence and odd-date items carry their tag.
- [ ] Refresh reloads the card in place.
- [ ] On a fake-data item needing review, Add to calendar creates an event (then Correct / Incorrect / Reschedule / Remove appear), and Don't add removes it from the list.
- [ ] Typing a date/time and pressing Reschedule on a needs-review item, or Schedule on an action item, creates an event at that time; leaving the field blank and pressing the button says so instead of doing nothing silently.
- [ ] A verdict is given once: after Correct or Incorrect, Reschedule and Remove are still offered.
- [ ] Set `API_BASE_URL` wrong: an error card names the problem and what to do.
- [ ] Stop the API: a "could not reach" card, not a blank panel.

## Security

- The API accepts a request only with a genuine Google identity token, issued for this client id, for
  the one allowed email. With either setting unset it refuses everything.
- **Never point the tunnel at `app.main`** (the dashboard has no login). Use `app.addon_app`.
- Develop against the fake-data database: the tunnel is public.
- Apps Script can restrict which hosts it may call (`urlFetchWhitelist` in the manifest). It is left
  open while the tunnel address keeps changing; set it to the real API host once that is fixed.

## Troubleshooting

| Card says | Likely cause |
|---|---|
| "API_BASE_URL is not set" | Step 7 |
| "refused this Google account" (401 / 403) | `ADDON_OAUTH_CLIENT_ID` does not match the `aud` from step 4, or `ADDON_ALLOWED_EMAIL` is not the account signed in to Gmail |
| "not configured yet" (503) | Either API setting is empty |
| "Could not reach the API" | The API or the tunnel is down, or the address changed |
| "No identity token" | The manifest's `openid` / `userinfo.email` scopes were not granted: re-run `debugToken` and approve |

## Next

The card for the open email (what was extracted from it, with the same buttons), and deploying the API
as a Lambda function URL in the SAM stack so it does not depend on a laptop. That needs the CI deploy
role's permissions extended first.
