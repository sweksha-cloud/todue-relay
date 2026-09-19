"""AWS Lambda entry point for the scheduled pipeline.

A thin wrapper: the extraction/pipeline logic is app.pipeline.run_pipeline,
unchanged. This file only does the three things Lambda needs that a plain
`python -m scripts.run_pipeline` on GitHub Actions did not:

1. Load the Gemini key and DB connection string from AWS Secrets Manager
   (instead of GitHub Actions secrets injected as env vars).
2. Do that BEFORE app.config is imported — config.py and db/session.py read
   the environment at import time, so importing them first would freeze in
   empty values.
3. Accept a manual/test event: {"dry_run": true} runs the pipeline's
   --dry-run mode (zero Gemini calls, zero writes).

Lambda handler string: `lambda_handler.handler`.

Environment variables (plain config, not secrets):
    SECRET_ID   name or ARN of the Secrets Manager secret. Its value is a JSON
                object with GEMINI_API_KEY and DATABASE_URL. If unset, nothing
                is fetched and the environment is used as-is (local runs).
Everything else app/config.py reads (FILTER_LEVEL, CALENDAR_TIMEZONE, ...) is
set as an ordinary Lambda environment variable.
"""

from __future__ import annotations

import json
import logging
import os

SECRET_KEYS = ("GEMINI_API_KEY", "DATABASE_URL")

logger = logging.getLogger(__name__)
_initialized = False


def load_secrets(client=None) -> None:
    """Copy SECRET_KEYS from the Secrets Manager secret named by SECRET_ID
    into os.environ. A no-op if SECRET_ID is unset. `client` is injectable
    for tests; by default a real boto3 client, whose credentials come from
    the Lambda's execution role (no access key is stored anywhere).
    """
    secret_id = os.environ.get("SECRET_ID")
    if not secret_id:
        return

    if client is None:
        import boto3  # provided by the Lambda Python runtime; imported lazily

        client = boto3.client("secretsmanager")

    payload = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    missing = [key for key in SECRET_KEYS if key not in payload]
    if missing:
        # Names only, never values.
        raise KeyError(f"Secret {secret_id!r} is missing required key(s): {', '.join(missing)}")
    for key in SECRET_KEYS:
        os.environ[key] = payload[key]


def _configure_logging() -> None:
    # Lambda's runtime pre-installs a root handler, which makes basicConfig a
    # no-op — so set the level directly. basicConfig only covers a local run.
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
    root.setLevel(logging.INFO)


def _init_once() -> None:
    """Runs on a cold start only; a warm container skips it."""
    global _initialized
    if _initialized:
        return
    _configure_logging()
    load_secrets()
    _initialized = True


def handler(event, context):
    """Run one pipeline pass. Returns run_pipeline's summary dict. A failed
    run raises (run_pipeline re-raises after recording the failure), so
    Lambda reports it as an error.

    event: {"dry_run": true} for a check that spends no Gemini quota. Anything
    else — including the scheduler's own payload — is a real run. Only a
    literal JSON `true` counts, so the string "false" can't switch it on.
    """
    _init_once()

    # Imported here, after the secrets are in the environment — see module docstring.
    from app.pipeline import run_pipeline

    dry_run = isinstance(event, dict) and event.get("dry_run") is True
    logger.info("Pipeline invoked (dry_run=%s)", dry_run)
    return run_pipeline(dry_run=dry_run)
