#!/usr/bin/env bash
# Starts the Gmail add-on's API against a FAKE-data database and gives it a public HTTPS address, so
# Apps Script (which runs on Google's servers and cannot reach localhost) can call it. Ctrl-C stops both.
#
#   PREVIEW_DATABASE_URL=<fake-data database> \
#   ADDON_OAUTH_CLIENT_ID=<the aud that debugToken printed> \
#   ADDON_ALLOWED_EMAIL=<your Google email> \
#   addon/scripts/dev.sh
#
# Needs: cloudflared (brew install cloudflared) and the backend virtualenv (backend/.venv).
set -euo pipefail

: "${PREVIEW_DATABASE_URL:?Set PREVIEW_DATABASE_URL to the FAKE-data database (never the real one: the address is public)}"
: "${ADDON_OAUTH_CLIENT_ID:?Set ADDON_OAUTH_CLIENT_ID to the aud that debugToken printed}"
: "${ADDON_ALLOWED_EMAIL:?Set ADDON_ALLOWED_EMAIL to your Google email}"

# A guard, not a guarantee: refuse unless the database name says it is the preview one.
case "$PREVIEW_DATABASE_URL" in
  *preview*) ;;
  *) if [ "${ALLOW_REAL_DATA:-}" != "1" ]; then
       echo "Refusing: PREVIEW_DATABASE_URL does not look like a preview database. The tunnel is public." >&2
       echo "If you really mean it, set ALLOW_REAL_DATA=1." >&2
       exit 1
     fi ;;
esac

HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(cd "$HERE/../../backend" && pwd)"
PORT="${PORT:-8002}"
LOG="$(mktemp)"

cleanup() { kill "${API_PID:-}" "${TUNNEL_PID:-}" 2>/dev/null || true; rm -f "$LOG"; }
trap cleanup EXIT INT TERM

echo "Starting the add-on API on port $PORT (its own app: the dashboard is NOT served)..."
(
  cd "$BACKEND"
  # shellcheck disable=SC1091
  source .venv/bin/activate
  DATABASE_URL="$PREVIEW_DATABASE_URL" \
  ADDON_OAUTH_CLIENT_ID="$ADDON_OAUTH_CLIENT_ID" \
  ADDON_ALLOWED_EMAIL="$ADDON_ALLOWED_EMAIL" \
  exec uvicorn app.addon_app:app --host 127.0.0.1 --port "$PORT" --log-level warning
) &
API_PID=$!

for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/addon/summary" || true)" = "401" ] && break
  sleep 1
done
[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/addon/summary" || true)" = "401" ] \
  || { echo "The API did not start." >&2; exit 1; }

echo "Opening a public tunnel..."
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate >"$LOG" 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 40); do
  URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 1
done
[ -n "$URL" ] || { echo "The tunnel did not give an address." >&2; exit 1; }

echo
echo "  API_BASE_URL  =  $URL"
echo
echo "Paste that into the Apps Script project's Script properties (Project Settings) as API_BASE_URL,"
echo "then reload Gmail. The address changes each time this script restarts. Ctrl-C to stop."
wait "$API_PID"
