import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { renderError, renderHome } from "../src/cards";
import { buildHomeModel, dateFieldName, errorModel } from "../src/format";
import { count, fakeCardService, paramsOf, texts, type Recorder } from "./fakes";
import { email, summary } from "./fixtures";

beforeEach(() => {
  (globalThis as Record<string, unknown>).CardService = fakeCardService;
});
afterEach(() => {
  delete (globalThis as Record<string, unknown>).CardService;
});

describe("renderHome", () => {
  it("shows the last run in the header and a section title for each list", () => {
    const run = { status: "success", started_text: "Mon Sep 21, 6:00 AM", fetched: 24, processed: 3, failed: 0 };
    const card = renderHome(buildHomeModel(summary({ latest_run: run }))) as unknown as Recorder;

    const all = texts(card);
    expect(all).toContain("ToDue Relay");
    expect(all.some((t) => t.includes("Mon Sep 21, 6:00 AM"))).toBe(true);
    expect(all).toEqual(expect.arrayContaining(["Needs review (0)", "Coming up (0)", "Action items (0)"]));
  });

  it("lists each item's title, deadline and warning tag", () => {
    const model = buildHomeModel(
      summary({
        counts: { needs_review: 1, upcoming: 0, action_items: 0 },
        needs_review: [email({ event_name: "Workshop signup", deadline_text: "Fri Sep 25", confidence: "low" })],
      }),
    );

    const all = texts(renderHome(model));

    expect(all).toEqual(expect.arrayContaining(["Workshop signup", "Fri Sep 25", "Low confidence"]));
  });

  it("says a section is empty instead of leaving it blank", () => {
    const all = texts(renderHome(buildHomeModel(summary())));

    expect(all).toEqual(expect.arrayContaining(["Nothing needs review.", "Nothing coming up.", "No action items."]));
  });

  it("notes when more items exist than are shown", () => {
    const model = buildHomeModel(summary({ counts: { needs_review: 12, upcoming: 0, action_items: 0 }, needs_review: [email()] }));

    expect(texts(renderHome(model))).toContain("+ 11 more in the dashboard");
  });

  it("builds one section per list and one item widget per item", () => {
    const model = buildHomeModel(
      summary({ counts: { needs_review: 0, upcoming: 2, action_items: 0 }, upcoming: [email({ email_id: "a" }), email({ email_id: "b" })] }),
    );
    const card = renderHome(model);

    expect(count(card, "newCardSection")).toBe(3);
    expect(count(card, "newDecoratedText")).toBe(2);
  });

  it("has a Refresh button wired to onRefresh, which the entry points expose", () => {
    const all = texts(renderHome(buildHomeModel(summary())));

    expect(all).toContain("Refresh");
    expect(all).toContain("onRefresh");
  });
});

describe("renderHome buttons", () => {
  const withActions = (actions: string[]) =>
    renderHome(buildHomeModel(summary({ counts: { needs_review: 1, upcoming: 0, action_items: 0 }, needs_review: [email({ email_id: "r1", actions })] })));

  it("draws a button for each offered action, in one row under the item", () => {
    const card = withActions(["approve", "decline"]);

    expect(texts(card)).toEqual(expect.arrayContaining(["Add to calendar", "Don't add"]));
    expect(count(card, "newButtonSet")).toBe(1);
    expect(count(card, "newTextButton")).toBe(3); // the two item buttons, plus Refresh in the footer
  });

  it("wires each button to onAction with the item's id and the action", () => {
    const card = withActions(["approve", "decline"]);

    expect(paramsOf(card)).toEqual([
      { emailId: "r1", action: "approve" },
      { emailId: "r1", action: "decline" },
    ]);
    expect(texts(card)).toContain("onAction");
  });

  it("draws no button row for an item with nothing to do", () => {
    expect(count(withActions([]), "newButtonSet")).toBe(0);
  });

  it("draws a date/time field, named for the item, above the buttons when one of them needs it", () => {
    const card = withActions(["approve_at", "decline"]);

    expect(count(card, "newTextInput")).toBe(1);
    expect(texts(card)).toContain(dateFieldName("r1"));
  });

  it("draws no date/time field when nothing offered needs one", () => {
    expect(count(withActions(["approve", "decline"]), "newTextInput")).toBe(0);
  });

  it("draws only one date/time field even when two buttons on the same item both need one", () => {
    // Not a case the API actually offers today (approve_at and reschedule never co-occur), but the
    // rendering rule is "one item, one field" regardless of how many of its buttons need it.
    expect(count(withActions(["approve_at", "reschedule"]), "newTextInput")).toBe(1);
  });
});

describe("renderError", () => {
  it("shows what went wrong and what to do about it", () => {
    const all = texts(renderError(errorModel("config", "API_BASE_URL is not set.")));

    expect(all).toContain("API_BASE_URL is not set.");
    expect(all.some((t) => t.includes("Script properties"))).toBe(true);
    expect(all).toContain("Refresh");
  });
});
