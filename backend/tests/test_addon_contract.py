"""The add-on is written against a committed snapshot of the API's response shapes
(addon/contract/api.schema.json). This fails when the API's models drift from that snapshot, which
would otherwise only show up as a broken card in Gmail. The add-on's own tests check their fixtures
against the same file, so a change has to be made on both sides on purpose."""

import json

from scripts.export_addon_schema import SCHEMA_PATH, build_schema


def test_the_committed_schema_matches_the_apis_models():
    committed = json.loads(SCHEMA_PATH.read_text())

    assert committed == build_schema(), (
        "The add-on API's response models changed. Regenerate the snapshot with "
        "`python -m scripts.export_addon_schema`, then update addon/src/types.ts and the add-on tests."
    )


def test_the_snapshot_describes_every_field_the_add_on_reads():
    schema = json.loads(SCHEMA_PATH.read_text())
    email = schema["Summary"]["$defs"]["EmailView"]["properties"]

    assert {"email_id", "subject", "event_name", "status", "deadline_text", "vote", "actions"} <= set(email)
    assert set(schema["ActionResult"]["properties"]) == {"ok", "message", "email"}
