import { describe, expect, it } from "vitest";
import { ACTION_LABELS, buildHomeModel, dateFieldName, errorModel, itemFor, moreText, parseActionParameters, runLine, truncate } from "../src/format";
import { email, summary } from "./fixtures";

describe("truncate", () => {
  it("leaves short text alone", () => expect(truncate("Rent due", 20)).toBe("Rent due"));
  it("cuts long text to the limit, ending in an ellipsis", () => {
    const out = truncate("a".repeat(100), 10);
    expect(out).toHaveLength(10);
    expect(out.endsWith("…")).toBe(true);
  });
  it("does not leave a space before the ellipsis", () => expect(truncate("hello world again", 12)).toBe("hello world…"));
});

describe("itemFor", () => {
  it("prefers the extracted event name over the raw subject", () => {
    expect(itemFor(email({ event_name: "Rent due", subject: "RE: fwd: your invoice" })).title).toBe("Rent due");
  });
  it("falls back to the subject, then to a placeholder", () => {
    expect(itemFor(email({ event_name: null, subject: "Team standup" })).title).toBe("Team standup");
    expect(itemFor(email({ event_name: "  ", subject: "  " })).title).toBe("(no subject)");
  });
  it("carries the server's wording of the deadline through untouched", () => {
    expect(itemFor(email({ deadline_text: "Thu Sep 24" })).subtitle).toBe("Thu Sep 24");
    expect(itemFor(email({ deadline_text: null })).subtitle).toBeNull();
  });
  it("flags a low-confidence item, and an odd date more urgently", () => {
    expect(itemFor(email({ confidence: "low" })).tag).toBe("Low confidence");
    expect(itemFor(email({ confidence: "low", is_implausible_date: true })).tag).toBe("Date looks odd");
    expect(itemFor(email({ confidence: "high" })).tag).toBeNull();
  });
  it("shortens a very long title", () => {
    expect(itemFor(email({ event_name: "x".repeat(200) })).title.length).toBeLessThanOrEqual(60);
  });
});

describe("moreText", () => {
  it("says how many are not shown", () => expect(moreText(16, 10)).toBe("+ 6 more in the dashboard"));
  it("says nothing when everything is shown", () => {
    expect(moreText(3, 3)).toBeNull();
    expect(moreText(0, 0)).toBeNull();
  });
});

describe("runLine", () => {
  const run = { status: "success", started_text: "Mon Sep 21, 6:00 AM", fetched: 24, processed: 3, failed: 0 };
  it("summarises a good run", () => expect(runLine(run)).toBe("Last run Mon Sep 21, 6:00 AM: ok, 24 fetched"));
  it("mentions failures only when there were some", () => {
    expect(runLine({ ...run, failed: 2 })).toContain(", 2 failed");
    expect(runLine(run)).not.toContain("failed");
  });
  it("shows a failed run's status plainly", () => expect(runLine({ ...run, status: "failure" })).toContain("failure"));
  it("copes with no runs at all", () => expect(runLine(null)).toBe("No runs yet"));
});

describe("buildHomeModel", () => {
  it("has the three sections in a fixed order, titled with the true totals", () => {
    const model = buildHomeModel(
      summary({ counts: { needs_review: 16, upcoming: 4, action_items: 0 }, needs_review: [email()] }),
    );

    expect(model.sections.map((s) => s.title)).toEqual(["Needs review (16)", "Coming up (4)", "Action items (0)"]);
  });
  it("notes how many needs-review items are beyond the ones shown", () => {
    const model = buildHomeModel(summary({ counts: { needs_review: 12, upcoming: 0, action_items: 0 }, needs_review: [email(), email()] }));

    expect(model.sections[0]?.more).toBe("+ 10 more in the dashboard");
  });
  it("gives each empty section its own message", () => {
    const model = buildHomeModel(summary());

    expect(model.sections.map((s) => s.emptyText)).toEqual(["Nothing needs review.", "Nothing coming up.", "No action items."]);
    expect(model.sections.every((s) => s.items.length === 0 && s.more === null)).toBe(true);
  });
  it("puts the last run in the subtitle", () => {
    const run = { status: "success", started_text: "Mon Sep 21, 6:00 AM", fetched: 1, processed: 0, failed: 0 };

    expect(buildHomeModel(summary({ latest_run: run })).subtitle).toContain("Mon Sep 21, 6:00 AM");
  });
});

describe("errorModel", () => {
  it.each(["config", "auth", "network", "server", "parse"] as const)("gives %s errors a message and an actionable hint", (kind) => {
    const m = errorModel(kind, "something");

    expect(m.message).toBe("something");
    expect(m.hint.length).toBeGreaterThan(20);
  });
  it("points an auth failure at the two settings that must match", () => {
    expect(errorModel("auth", "x").hint).toContain("ADDON_OAUTH_CLIENT_ID");
  });
});

describe("item buttons", () => {
  it("draws exactly the buttons the API offered, in its order, with friendly labels", () => {
    const item = itemFor(email({ email_id: "r1", actions: ["approve", "decline"] }));

    expect(item.buttons).toEqual([
      { label: "Add to calendar", emailId: "r1", action: "approve" },
      { label: "Don't add", emailId: "r1", action: "decline" },
    ]);
  });
  it("has none when the API offered none", () => expect(itemFor(email({ actions: [] })).buttons).toEqual([]));
  it("skips an action this version does not know, instead of drawing a dead button", () => {
    expect(itemFor(email({ actions: ["snooze", "remove"] })).buttons.map((b) => b.action)).toEqual(["remove"]);
  });
  it("has a label for every action the API can offer", () => {
    expect(Object.keys(ACTION_LABELS).sort()).toEqual([
      "approve", "approve_at", "decline", "remove", "reschedule", "schedule", "vote_correct", "vote_incorrect",
    ]);
  });
  it("carries the item's own email id, for the date field and for every button", () => {
    const item = itemFor(email({ email_id: "r1", actions: ["remove"] }));
    expect(item.emailId).toBe("r1");
  });
  it("asks for a date/time field when a button needs one, and not otherwise", () => {
    expect(itemFor(email({ actions: ["approve", "decline"] })).needsDatetime).toBe(false);
    expect(itemFor(email({ actions: ["approve_at", "decline"] })).needsDatetime).toBe(true);
    expect(itemFor(email({ actions: ["reschedule", "remove"] })).needsDatetime).toBe(true);
    expect(itemFor(email({ actions: ["schedule", "decline"] })).needsDatetime).toBe(true);
  });
});

describe("the verdict tag", () => {
  it("shows a verdict already given", () => {
    expect(itemFor(email({ vote: "correct" })).tag).toBe("Marked correct");
    expect(itemFor(email({ vote: "incorrect" })).tag).toBe("Marked incorrect");
  });
  it("still puts a warning ahead of a verdict", () => expect(itemFor(email({ vote: "correct", confidence: "low" })).tag).toBe("Low confidence"));
});

describe("parseActionParameters", () => {
  it("reads a button press that needs no date", () => {
    expect(parseActionParameters({ emailId: "e1", action: "remove" })).toEqual({ ok: true, emailId: "e1", action: "remove" });
  });
  it.each([
    ["nothing", undefined],
    ["a string", "remove"],
    ["a missing id", { action: "remove" }],
    ["an empty id", { emailId: "", action: "remove" }],
    ["an unknown action", { emailId: "e1", action: "explode" }],
    ["a non-string id", { emailId: 5, action: "remove" }],
  ])("rejects %s as unrecognised", (_name, value) => {
    expect(parseActionParameters(value)).toEqual({ ok: false, reason: "unrecognised" });
  });

  describe("an action that needs a date/time", () => {
    it("reads it from that item's own field in formInput", () => {
      const formInput = { [dateFieldName("e1")]: "2026-10-05 14:30" };

      expect(parseActionParameters({ emailId: "e1", action: "reschedule" }, formInput)).toEqual({
        ok: true, emailId: "e1", action: "reschedule", newDatetime: "2026-10-05 14:30",
      });
    });
    it("trims surrounding whitespace", () => {
      const formInput = { [dateFieldName("e1")]: "  2026-10-05 14:30  " };
      const result = parseActionParameters({ emailId: "e1", action: "schedule" }, formInput);

      expect(result).toEqual({ ok: true, emailId: "e1", action: "schedule", newDatetime: "2026-10-05 14:30" });
    });
    it("only reads that item's own field, not another item's", () => {
      const formInput = { [dateFieldName("other-item")]: "2026-10-05 14:30" };

      expect(parseActionParameters({ emailId: "e1", action: "reschedule" }, formInput)).toEqual({ ok: false, reason: "missing_datetime" });
    });
    it.each([
      ["formInput is missing entirely", undefined],
      ["the field is missing", {}],
      ["the field is blank", { [dateFieldName("e1")]: "" }],
      ["the field is only whitespace", { [dateFieldName("e1")]: "   " }],
    ])("reports missing_datetime when %s", (_name, formInput) => {
      expect(parseActionParameters({ emailId: "e1", action: "approve_at" }, formInput)).toEqual({ ok: false, reason: "missing_datetime" });
    });
  });
});
