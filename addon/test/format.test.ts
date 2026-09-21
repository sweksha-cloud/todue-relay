import { describe, expect, it } from "vitest";
import { buildHomeModel, errorModel, itemFor, moreText, runLine, truncate } from "../src/format";
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
