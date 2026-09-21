"""Telling a person when something needs their attention, by email, through SNS.

Runs report SUCCESS whenever the run itself completes, so an email that fails every time, hour after hour,
looks healthy from outside: three good emails once sat parked for days before anyone noticed. This module
is how the pipeline says so. It reuses the SNS topic the CloudWatch alarms already email (set as
ALERT_TOPIC_ARN by the SAM template), so there is nothing new to subscribe to. With no topic configured
(a local run, GitHub Actions) an alert is only logged. Sending one must never fail a run, so nothing
here raises.
"""

from __future__ import annotations

import logging

from app import config

logger = logging.getLogger(__name__)

MAX_LISTED = 10  # one alert per run however many emails were parked; the rest are counted, not listed


def _ascii_subject(text: str) -> str:
    # SNS email subjects must be ASCII, on one line, under 100 characters.
    return " ".join(text.encode("ascii", "ignore").decode().split())[:100]


def send_alert(subject: str, message: str, client=None) -> bool:
    """Email `message` to whoever is subscribed to the alert topic. True if it was handed to SNS.
    `client` is injectable for tests; by default a real boto3 SNS client, with credentials from
    the Lambda's execution role."""
    topic = config.ALERT_TOPIC_ARN
    if not topic:
        logger.warning("Alert not sent (no ALERT_TOPIC_ARN): %s", subject)
        return False
    try:
        if client is None:
            import boto3  # bundled in the Lambda package; lazy so local runs and tests need no boto3

            client = boto3.client("sns")
        client.publish(TopicArn=topic, Subject=_ascii_subject(subject), Message=message)
        return True
    except Exception:  # noqa: BLE001 - an alert that cannot be sent must not fail the run
        logger.exception("Could not send alert: %s", subject)
        return False


def notify_parked(parked: list[dict], max_attempts: int | None = None, client=None) -> bool:
    """One alert for the emails that failed for good in this run. Each item: {id, subject, error}."""
    if not parked:
        return False
    attempts = max_attempts if max_attempts is not None else config.MAX_ATTEMPTS_PER_EMAIL
    n = len(parked)
    subject = f"ToDue Relay: {n} email{'s' if n != 1 else ''} failed and will not be retried"
    lines = [f"{n} email{'s' if n != 1 else ''} used all {attempts} attempts and will not be retried automatically:", ""]
    for item in parked[:MAX_LISTED]:
        lines.append(f"- {item['subject'] or '(no subject)'}")
        lines.append(f"  {(item['error'] or 'no error recorded')[:200]}")
    if n > MAX_LISTED:
        lines.append(f"... and {n - MAX_LISTED} more.")
    lines += [
        "",
        "If the error is a temporary Gemini one (503 or 429), this gives them fresh attempts:",
        "  python -m scripts.retry_failed --apply",
        "They also show as failed on the dashboard.",
    ]
    return send_alert(subject, "\n".join(lines), client=client)
