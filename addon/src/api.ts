// Fetching the summary from the ToDue Relay API. Google's services are passed in (ApiDeps) instead of
// called directly, so the logic here, including every way it can fail, is unit tested with fakes.

import type { ErrorKind } from "./format";
import type { Summary } from "./types";

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

export interface ApiDeps {
  getToken(): string | null;
  getProperty(name: string): string | null;
  fetch(url: string, headers: Record<string, string>): { status: number; body: string };
}

export const SUMMARY_PATH = "/api/addon/summary";

export function fetchSummary(deps: ApiDeps): Summary {
  const base = deps.getProperty("API_BASE_URL");
  if (!base) throw new ApiError("config", "API_BASE_URL is not set.");

  const token = deps.getToken();
  if (!token) throw new ApiError("auth", "No identity token: the add-on needs the openid and email scopes.");

  let res: { status: number; body: string };
  try {
    res = deps.fetch(base.replace(/\/+$/, "") + SUMMARY_PATH, { Authorization: `Bearer ${token}` });
  } catch (e) {
    throw new ApiError("network", `Could not reach the API: ${e instanceof Error ? e.message : String(e)}`);
  }

  if (res.status === 401 || res.status === 403) {
    throw new ApiError("auth", `The API refused this Google account (HTTP ${res.status}).`, res.status);
  }
  if (res.status === 503) {
    throw new ApiError("server", "The API is not configured yet, or cannot verify tokens right now (HTTP 503).", 503);
  }
  if (res.status !== 200) throw new ApiError("server", `The API answered HTTP ${res.status}.`, res.status);

  try {
    return JSON.parse(res.body) as Summary;
  } catch {
    throw new ApiError("parse", "The API's response was not valid JSON.");
  }
}

/** The real thing, wired to Apps Script's services. Only exercised inside Gmail. */
export function appsScriptDeps(): ApiDeps {
  return {
    getToken: () => ScriptApp.getIdentityToken(),
    getProperty: (name) => PropertiesService.getScriptProperties().getProperty(name),
    fetch: (url, headers) => {
      const r = UrlFetchApp.fetch(url, { headers, muteHttpExceptions: true, followRedirects: false });
      return { status: r.getResponseCode(), body: r.getContentText() };
    },
  };
}
