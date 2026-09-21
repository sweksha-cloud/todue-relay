import { describe, expect, it } from "vitest";
import { ApiError, fetchSummary, type ApiDeps } from "../src/api";
import { summary } from "./fixtures";

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
    let seen: { url: string; headers: Record<string, string> } | undefined;
    fetchSummary(deps({ fetch: (url, headers) => ((seen = { url, headers }), { status: 200, body: "{}" }) }));

    expect(seen?.url).toBe("https://api.example.com/api/addon/summary");
    expect(seen?.headers).toEqual({ Authorization: "Bearer the-token" });
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
