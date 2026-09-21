import { describe, expect, it } from "vitest";
import { ACTION_ROUTES, ApiError, fetchSummary, runAction, type ApiDeps, type FetchOptions } from "../src/api";
import { actionResult, summary } from "./fixtures";

function deps(overrides: Partial<ApiDeps> = {}): ApiDeps {
  return {
    getToken: () => "the-token",
    getProperty: (name) => (name === "API_BASE_URL" ? "https://api.example.com" : null),
    fetch: () => ({ status: 200, body: JSON.stringify(summary()) }),
    ...overrides,
  };
}

function failure(d: ApiDeps): ApiError {
  try {
    fetchSummary(d);
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected fetchSummary to throw");
}

describe("fetchSummary", () => {
  it("returns the parsed summary on success", () => {
    expect(fetchSummary(deps()).counts.needs_review).toBe(0);
  });

  it("calls the summary endpoint with the identity token as a bearer token", () => {
    let seen: { url: string; options: FetchOptions } | undefined;
    fetchSummary(deps({ fetch: (url, options) => ((seen = { url, options }), { status: 200, body: "{}" }) }));

    expect(seen?.url).toBe("https://api.example.com/api/addon/summary");
    expect(seen?.options).toEqual({ method: "GET", headers: { Authorization: "Bearer the-token" } });
  });

  it("copes with a trailing slash on the configured address", () => {
    let url = "";
    fetchSummary(deps({ getProperty: () => "https://api.example.com//", fetch: (u) => ((url = u), { status: 200, body: "{}" }) }));

    expect(url).toBe("https://api.example.com/api/addon/summary");
  });

  it("reports a missing address as a config problem, without calling anything", () => {
    let called = false;
    const err = failure(deps({ getProperty: () => null, fetch: () => ((called = true), { status: 200, body: "{}" }) }));

    expect(err.kind).toBe("config");
    expect(called).toBe(false);
  });

  it("reports a missing identity token as an auth problem, without calling the API", () => {
    let called = false;
    const err = failure(deps({ getToken: () => null, fetch: () => ((called = true), { status: 200, body: "{}" }) }));

    expect(err.kind).toBe("auth");
    expect(called).toBe(false);
  });

  it.each([401, 403])("reports HTTP %i as an auth problem", (status) => {
    const err = failure(deps({ fetch: () => ({ status, body: "" }) }));

    expect(err.kind).toBe("auth");
    expect(err.status).toBe(status);
  });

  it("reports HTTP 503 as the API not being configured", () => {
    expect(failure(deps({ fetch: () => ({ status: 503, body: "" }) })).message).toContain("not configured");
  });

  it("reports other errors as server problems", () => {
    const err = failure(deps({ fetch: () => ({ status: 500, body: "" }) }));

    expect(err.kind).toBe("server");
    expect(err.status).toBe(500);
  });

  it("reports an unreachable API as a network problem", () => {
    const err = failure(deps({ fetch: () => { throw new Error("DNS error"); } }));

    expect(err.kind).toBe("network");
    expect(err.message).toContain("DNS error");
  });

  it("reports a non-JSON body as a parse problem", () => {
    expect(failure(deps({ fetch: () => ({ status: 200, body: "<html>" }) })).kind).toBe("parse");
  });

  it("never puts the token in an error message", () => {
    for (const status of [401, 500, 503]) {
      expect(failure(deps({ fetch: () => ({ status, body: "" }) })).message).not.toContain("the-token");
    }
  });
});

function post(d: Partial<ApiDeps> = {}) {
  const seen: Array<{ url: string; options: FetchOptions }> = [];
  const all = deps({ fetch: (url, options) => (seen.push({ url, options }), { status: 200, body: JSON.stringify(actionResult()) }), ...d });
  return { all, seen };
}

describe("runAction", () => {
  it("POSTs approve, decline and remove to their own endpoints, with the login header and no choice to make", () => {
    for (const action of ["approve", "decline", "remove"]) {
      const { all, seen } = post();

      runAction(all, "abc123", action);

      expect(seen[0]?.url).toBe(`https://api.example.com/api/addon/emails/abc123/${action}`);
      expect(seen[0]?.options.method).toBe("POST");
      expect(seen[0]?.options.headers.Authorization).toBe("Bearer the-token");
      expect(seen[0]?.options.payload).toBe("{}");
    }
  });

  it("sends a vote to the vote endpoint with the verdict in the body", () => {
    const { all, seen } = post();

    runAction(all, "abc123", "vote_correct");
    runAction(all, "abc123", "vote_incorrect");

    expect(seen.map((s) => s.url)).toEqual(Array(2).fill("https://api.example.com/api/addon/emails/abc123/vote"));
    expect(seen.map((s) => s.options.payload)).toEqual(['{"vote":"correct"}', '{"vote":"incorrect"}']);
    expect(seen[0]?.options.headers["Content-Type"]).toBe("application/json");
  });

  it("returns the API's message and the updated item", () => {
    const { all } = post();

    expect(runAction(all, "e1", "approve").message).toBe("Added to your calendar");
  });

  it("escapes an id so it cannot change the path", () => {
    const { all, seen } = post();

    runAction(all, "a/b?c", "remove");

    expect(seen[0]?.url).toBe("https://api.example.com/api/addon/emails/a%2Fb%3Fc/remove");
  });

  it("shows the API's own reason when it refuses an action (400)", () => {
    const err = failureOf(() => runAction(post({ fetch: () => ({ status: 400, body: '{"detail":"Not an approvable item"}' }) }).all, "e1", "approve"));

    expect(err.message).toBe("Not an approvable item");
    expect(err.status).toBe(400);
  });

  it("copes with an error body that is not JSON", () => {
    const err = failureOf(() => runAction(post({ fetch: () => ({ status: 404, body: "<html>" }) }).all, "e1", "remove"));

    expect(err.message).toBe("The API answered HTTP 404.");
  });

  it("refuses an action it does not know, without calling the API", () => {
    const { all, seen } = post();

    expect(failureOf(() => runAction(all, "e1", "delete_everything")).kind).toBe("parse");
    expect(seen).toHaveLength(0);
  });

  it("knows a route for every action the labels offer", () => {
    expect(Object.keys(ACTION_ROUTES).sort()).toEqual(["approve", "decline", "remove", "vote_correct", "vote_incorrect"]);
  });

  it("treats a refused account the same way as for the summary", () => {
    expect(failureOf(() => runAction(post({ fetch: () => ({ status: 403, body: "" }) }).all, "e1", "remove")).kind).toBe("auth");
  });
});

function failureOf(fn: () => unknown): ApiError {
  try {
    fn();
  } catch (e) {
    return e as ApiError;
  }
  throw new Error("expected a failure");
}
