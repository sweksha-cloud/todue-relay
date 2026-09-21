"""Sign in to Google again and store the new token — run this when the pipeline fails with
"invalid_grant: Token has been expired or revoked" (Google expires refresh tokens after 7 days
while the OAuth app is in 'Testing' status).

Opens a browser for Google's consent screen. The new token is saved to the database, which is
what the scheduled pipeline (AWS Lambda, or GitHub Actions) reads, so it starts working again on
its next run; a copy is also kept in backend/credentials/ for local runs.

Usage (from backend/, with .venv active):
    python -m scripts.reauth_google
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app.google_auth import reauthorize_google  # noqa: E402

if __name__ == "__main__":
    reauthorize_google()
    print("Signed in. The new token is saved to the database; the next scheduled run will use it.")
