"""Writes gmail-ui/contract/api.schema.json: the shapes the add-on API returns.

The add-on (gmail-ui/) is written in TypeScript against these shapes. The snapshot is committed, and a test
(tests/test_addon_contract.py) fails whenever the API's models no longer match it, so the API cannot
change shape without this file, and therefore the add-on's own tests, being brought along.

Run after changing a response model in app/addon_api.py:   python -m scripts.export_addon_schema
"""

from __future__ import annotations

import json
from pathlib import Path

from app.addon_api import ActionResult, Summary

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "gmail-ui" / "contract" / "api.schema.json"


def build_schema() -> dict:
    return {"Summary": Summary.model_json_schema(), "ActionResult": ActionResult.model_json_schema()}


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    SCHEMA_PATH.write_text(render(build_schema()))
    print(f"wrote {SCHEMA_PATH}")
