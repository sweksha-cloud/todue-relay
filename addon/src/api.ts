// Talking to the ToDue Relay API. Google's services are passed in (ApiDeps) instead of called directly,
// so the logic here, including every way it can fail, is unit tested with fakes.

import type { ErrorKind } from "./format";
import type { ActionResult, Summary } from "./types";

export class ApiError extends Error {
  constructor(
    readonly kind: ErrorKind,
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface FetchOptions {
  method: "GET" | "POST";
  headers: Record<string, string>;
  payload?: string;
}

export interface ApiDeps {
  getToken(): string | null;
  getProperty(name: string): string | null;
  fetch(url: string, options: FetchOptions): { status: number; body: string };
}

export const SUMMARY_PATH = "/api/addon/summary";

interface Route {
  path: (emailId: string) => string;
  body?: object;
}

const emailPath = (verb: string) => (emailId: string) => `/api/addon/emails/${encodeURIComponent(emailId)}/${verb}`;

/** Which endpoint each action the API can offer calls. */
export const ACTION_ROUTES: Record<string, Route> = {
  approve: { path: emailPath("approve") },
  decline: { path: emailPath("decline") },
  remove: { path: emailPath("remove") },
  vote_correct: { path: emailPath("vote"), body: { vote: "correct" } },
  vote_incorrect: { path: emailPath("vote"), body: { vote: "incorrect" } },
};

/** The API's own explanation when it refused (its error bodies look like {"detail": "..."}). */
function detailOf(body: string): string | null {
  try {
    const d = (JSON.parse(body) as { detail?: unknown }).detail;
    return typeof d === "string" ? d : null;
  } catch {
    return null;
  }
}

function call(deps: ApiDeps, path: string, method: "GET" | "POST", body?: object): string {
  const base = deps.getProperty("API_BASE_URL");
  if (!base) throw new ApiError("config", "API_BASE_URL is not set.");

  const token = deps.getToken();
  if (!token) throw new ApiError("auth", "No identity token: the add-on needs the openid and email scopes.");

  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  const options: FetchOptions = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.payload = JSON.stringify(body);
  }

  let res: { status: number; body: string };
  try {
    res = deps.fetch(base.replace(/\/+$/, "") + path, options);
  } catch (e) {
    throw new ApiError("network", `Could not reach the API: ${e instanceof Error ? e.message : String(e)}`);
  }

  if (res.status === 401 || res.status === 403) {
    throw new ApiError("auth", `The API refused this Google account (HTTP ${res.status}).`, res.status);
  }
  if (res.status === 503) {
    throw new ApiError("server", "The API is not configured yet, or cannot verify tokens right now (HTTP 503).", 503);
  }
  if (res.status !== 200) {
    // A refused action (400) or a vanished item (404) carries the API's own reason: show that.
    throw new ApiError("server", detailOf(res.body) ?? `The API answered HTTP ${res.status}.`, res.status);
  }
  return res.body;
}

function parse<T>(body: string): T {
  try {
    return JSON.parse(body) as T;
  } catch {
    throw new ApiError("parse", "The API's response was not valid JSON.");
  }
}

export function fetchSummary(deps: ApiDeps): Summary {
  return parse<Summary>(call(deps, SUMMARY_PATH, "GET"));
}

export function runAction(deps: ApiDeps, emailId: string, action: string): ActionResult {
  const route = ACTION_ROUTES[action];
  if (!route) throw new ApiError("parse", `This add-on does not know the action "${action}".`);
  return parse<ActionResult>(call(deps, route.path(emailId), "POST", route.body ?? {}));
}

/** The real thing, wired to Apps Script's services. Only exercised inside Gmail. */
export function appsScriptDeps(): ApiDeps {
  return {
    getToken: () => ScriptApp.getIdentityToken(),
    getProperty: (name) => PropertiesService.getScriptProperties().getProperty(name),
    fetch: (url, options) => {
      const params: GoogleAppsScript.URL_Fetch.URLFetchRequestOptions = {
        method: options.method === "POST" ? "post" : "get",
        headers: options.headers,
        muteHttpExceptions: true,
        followRedirects: false,
      };
      if (options.payload !== undefined) {
        params.payload = options.payload;
        params.contentType = "application/json";
      }
      const r = UrlFetchApp.fetch(url, params);
      return { status: r.getResponseCode(), body: r.getContentText() };
    },
  };
}
