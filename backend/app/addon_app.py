"""The Gmail add-on's API as its OWN application, separate from the dashboard (app/main.py).

Why separate: the dashboard has no authentication (it is meant to run on a laptop). If the add-on
routes lived in that app and it were given a public address, the whole dashboard would be public
with it. This app contains only the authenticated add-on routes, so exposing it (a tunnel while
developing, a function URL later) exposes nothing else. The interactive docs are switched off too.

Run locally:  uvicorn app.addon_app:app --port 8002
"""

from __future__ import annotations

from fastapi import FastAPI

from app.addon_api import router

app = FastAPI(title="ToDue Relay add-on API", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
