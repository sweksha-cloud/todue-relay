// The add-on is written against a committed snapshot of the API's response shapes
// (contract/api.schema.json, written by the backend's scripts/export_addon_schema.py, and kept
// honest by a backend test). This checks the add-on's fixtures have exactly the fields that snapshot
// describes, so a field added or removed on the API side fails HERE until types.ts and the fixtures
// are brought along, and not as a broken card in Gmail.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { actionResult, email, run, summary } from "./fixtures";

interface ObjectSchema {
  properties: Record<string, unknown>;
  required?: string[];
}
interface Schema {
  Summary: ObjectSchema & { $defs: Record<string, ObjectSchema> };
  ActionResult: ObjectSchema;
}

const schema = JSON.parse(readFileSync("contract/api.schema.json", "utf8")) as Schema;
const keys = (o: object) => Object.keys(o).sort();
const propertyNames = (s: ObjectSchema) => Object.keys(s.properties).sort();
const def = (name: string): ObjectSchema => {
  const d = schema.Summary.$defs[name];
  if (!d) throw new Error(`the schema has no ${name}`);
  return d;
};

describe("the add-on's fixtures match the API's published shapes", () => {
  it("Summary", () => expect(keys(summary())).toEqual(propertyNames(schema.Summary)));
  it("Summary.counts", () => expect(keys(summary().counts)).toEqual(propertyNames(def("Counts"))));
  it("EmailView", () => expect(keys(email())).toEqual(propertyNames(def("EmailView"))));
  it("RunView", () => expect(keys(run())).toEqual(propertyNames(def("RunView"))));
  it("ActionResult", () => expect(keys(actionResult())).toEqual(propertyNames(schema.ActionResult)));

  it("every field the API always sends is present in the fixtures (none are optional)", () => {
    for (const [name, fixture] of [["EmailView", email()], ["RunView", run()], ["Counts", summary().counts]] as const) {
      for (const required of def(name).required ?? []) expect(fixture, `${name}.${required}`).toHaveProperty(required);
    }
  });
});
