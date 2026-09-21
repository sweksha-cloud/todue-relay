// Runs the REAL bundled script (dist/Code.js) in a sandbox that stands in for Apps Script, and calls
// the entry points the way Gmail does. Unit tests on the modules cannot show that the bundle exposes
// onHomepage / onRefresh / debugToken as globals, or that the pieces are wired together correctly.

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { beforeAll, describe, expect, it } from "vitest";
import { called, fakeCardService, texts } from "./fakes";
import { actionResult, email, summary } from "./fixtures";

let code = "";
beforeAll(() => {
  execFileSync("node", ["build.mjs"], { stdio: "ignore" });
  code = readFileSync("dist/Code.js", "utf8");
});

interface World {
  fetches: Array<{ url: string; method: string; headers: Record<string, string>; payload?: string }>;
  logs: string[];
}

function load(opts: { status?: number; body?: unknown; props?: Record<string, string>; token?: string | null } = {}) {
  const world: World = { fetches: [], logs: [] };
  const props = opts.props ?? { API_BASE_URL: "https://api.example.com" };
  const sandbox: Record<string, unknown> = {
    CardService: fakeCardService,
    ScriptApp: { getIdentityToken: () => (opts.token === undefined ? "the-token" : opts.token) },
    PropertiesService: { getScriptProperties: () => ({ getProperty: (n: string) => props[n] ?? null }) },
    UrlFetchApp: {
      fetch: (url: string, options: { method?: string; headers: Record<string, string>; payload?: string }) => {
        world.fetches.push({ url, method: options.method ?? "get", headers: options.headers, payload: options.payload });
        return { getResponseCode: () => opts.status ?? 200, getContentText: () => (typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body ?? summary())) };
      },
    },
    Logger: { log: (line: string) => world.logs.push(line) },
    Utilities: {
      base64DecodeWebSafe: (s: string) => Buffer.from(s, "base64url"),
      newBlob: (bytes: Uint8Array) => ({ getDataAsString: () => Buffer.from(bytes).toString("utf8") }),
    },
  };
  vm.runInNewContext(code, sandbox);
  return {
    world,
    /** Calls a global function the way Gmail would. Fails clearly if the bundle did not expose it. */
    run: (name: string, event?: unknown): unknown => {
      const fn = sandbox[name];
      if (typeof fn !== "function") throw new Error(`${name} is not exposed as a global function`);
      return fn(event);
    },
    has: (name: string): boolean => typeof sandbox[name] === "function",
  };
}

describe("the bundled script, as Gmail runs it", () => {
  it("has no module syntax, because Apps Script has no module system", () => {
    expect(code).not.toMatch(/^(import|export) /m);
  });

  it("exposes the entry points the manifest names as plain global functions", () => {
    const { has } = load();

    expect(has("onHomepage")).toBe(true);
    expect(has("onRefresh")).toBe(true);
    expect(has("onAction")).toBe(true);
    expect(has("debugToken")).toBe(true);
  });

  it("onHomepage fetches the summary with the identity token and shows what came back", () => {
    const body = summary({
      counts: { needs_review: 1, upcoming: 0, action_items: 0 },
      needs_review: [email({ event_name: "Workshop signup", deadline_text: "Fri Sep 25" })],
    });
    const { run, world } = load({ body });

    const card = run("onHomepage");

    expect(world.fetches).toEqual([
      { url: "https://api.example.com/api/addon/summary", method: "get", headers: { Authorization: "Bearer the-token" }, payload: undefined },
    ]);
    expect(texts(card)).toEqual(expect.arrayContaining(["Workshop signup", "Fri Sep 25", "Needs review (1)"]));
  });

  it("onHomepage shows a helpful card, not a crash, when the API refuses this account", () => {
    const { run } = load({ status: 403 });

    const all = texts(run("onHomepage"));

    expect(all.some((t) => t.includes("refused"))).toBe(true);
    expect(all.some((t) => t.includes("ADDON_OAUTH_CLIENT_ID"))).toBe(true);
  });

  it("onHomepage tells you when the API address has not been set, without calling out", () => {
    const { run, world } = load({ props: {} });

    expect(texts(run("onHomepage")).some((t) => t.includes("API_BASE_URL"))).toBe(true);
    expect(world.fetches).toHaveLength(0);
  });

  it("onRefresh replaces the card in place", () => {
    const { run } = load();

    expect(called(run("onRefresh"), "updateCard")).toBe(true);
  });

  it("debugToken logs the client id and email to copy into the API's settings", () => {
    const payload = Buffer.from(JSON.stringify({ aud: "client-9.apps.googleusercontent.com", email: "me@example.com", iss: "google" })).toString("base64url");
    const { run, world } = load({ token: `x.${payload}.y` });

    run("debugToken");

    expect(world.logs.join("\n")).toContain("client-9.apps.googleusercontent.com");
    expect(world.logs.join("\n")).toContain("me@example.com");
  });

  it("debugToken says what is wrong when there is no token", () => {
    const { run, world } = load({ token: null });

    run("debugToken");

    expect(world.logs.join("\n")).toContain("openid");
  });
});

describe("pressing a button, as Gmail runs it", () => {
  const press = (action: string, emailId = "r1") => ({ parameters: { emailId, action } });

  it("POSTs the action to the API, tells the person what happened, and redraws the card", () => {
    const { run, world } = load({ body: actionResult({ message: "Added to your calendar" }) });

    const response = run("onAction", press("approve"));

    expect(world.fetches[0]).toMatchObject({ url: "https://api.example.com/api/addon/emails/r1/approve", method: "post", headers: { Authorization: "Bearer the-token" } });
    expect(texts(response)).toContain("Added to your calendar");
    expect(called(response, "setNotification")).toBe(true);
    expect(called(response, "updateCard")).toBe(true);
  });

  it("sends a vote's verdict as JSON", () => {
    const { run, world } = load({ body: actionResult({ message: "Marked correct" }) });

    run("onAction", press("vote_correct", "c1"));

    expect(world.fetches[0]).toMatchObject({ url: "https://api.example.com/api/addon/emails/c1/vote", method: "post", payload: '{"vote":"correct"}' });
  });

  it("tells the person the API's own reason when it refuses, and leaves the card alone", () => {
    const { run } = load({ status: 400, body: '{"detail":"Not an approvable item"}' });

    const response = run("onAction", press("approve"));

    expect(texts(response)).toContain("Not an approvable item");
    expect(called(response, "updateCard")).toBe(false);
  });

  it("does not call the API for a press it does not recognise", () => {
    const { run, world } = load();

    const response = run("onAction", { parameters: { emailId: "r1", action: "explode" } });

    expect(world.fetches).toHaveLength(0);
    expect(texts(response)).toContain("That button was not recognised.");
  });

  it("copes with a press that carries no parameters at all", () => {
    const { run, world } = load();

    expect(texts(run("onAction", {}))).toContain("That button was not recognised.");
    expect(world.fetches).toHaveLength(0);
  });

  it("tells the person when the API cannot be reached, instead of failing silently", () => {
    const { run } = load({ props: {} });

    expect(texts(run("onAction", press("remove")))).toContain("API_BASE_URL is not set.");
  });
});
