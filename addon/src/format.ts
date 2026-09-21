// Everything that DECIDES how the home card reads lives here as plain functions of plain data, so
// it can be unit tested. Apps Script is a poor place to debug: the card can only be seen in Gmail.
// cards.ts turns this model into Google's card widgets and holds no decisions of its own.

import type { EmailView, RunView, Summary } from "./types";

export const ACTION_LABELS: Record<string, string> = {
  approve: "Add to calendar",
  decline: "Don't add",
  vote_correct: "Correct",
  vote_incorrect: "Incorrect",
  remove: "Remove",
};

export interface ItemButton {
  label: string;
  emailId: string;
  action: string;
}

export interface HomeItem {
  title: string;
  subtitle: string | null;
  tag: string | null;
  buttons: ItemButton[];
}

export interface HomeSection {
  title: string;
  emptyText: string;
  items: HomeItem[];
  more: string | null;
}

export interface HomeModel {
  title: string;
  subtitle: string;
  sections: HomeSection[];
}

const MAX_TITLE = 60;

export function truncate(text: string, max: number = MAX_TITLE): string {
  return text.length <= max ? text : `${text.slice(0, max - 1).trimEnd()}…`;
}

export function itemFor(e: EmailView): HomeItem {
  const name = (e.event_name ?? "").trim();
  const title = truncate(name || e.subject.trim() || "(no subject)");
  let tag: string | null = null;
  if (e.is_implausible_date) tag = "Date looks odd";
  else if (e.confidence === "low") tag = "Low confidence";
  else if (e.vote) tag = e.vote === "correct" ? "Marked correct" : "Marked incorrect";
  // Only the actions the API offered, in its order; one it offers that this version does not know is skipped.
  const buttons = e.actions.filter((a) => a in ACTION_LABELS).map((a) => ({ label: ACTION_LABELS[a] as string, emailId: e.email_id, action: a }));
  return { title, subtitle: e.deadline_text, tag, buttons };
}

export function moreText(total: number, shown: number): string | null {
  const rest = total - shown;
  return rest > 0 ? `+ ${rest} more in the dashboard` : null;
}

export function runLine(run: RunView | null): string {
  if (!run) return "No runs yet";
  const outcome = run.status === "success" ? "ok" : run.status;
  const failed = run.failed > 0 ? `, ${run.failed} failed` : "";
  return `Last run ${run.started_text}: ${outcome}, ${run.fetched} fetched${failed}`;
}

function section(title: string, count: number, items: EmailView[], emptyText: string): HomeSection {
  return {
    title: `${title} (${count})`,
    emptyText,
    items: items.map(itemFor),
    more: moreText(count, items.length),
  };
}

export function buildHomeModel(s: Summary): HomeModel {
  return {
    title: "ToDue Relay",
    subtitle: runLine(s.latest_run),
    sections: [
      section("Needs review", s.counts.needs_review, s.needs_review, "Nothing needs review."),
      section("Coming up", s.counts.upcoming, s.upcoming, "Nothing coming up."),
      section("Action items", s.counts.action_items, s.action_items, "No action items."),
    ],
  };
}

export type ErrorKind = "config" | "auth" | "network" | "server" | "parse";

export interface ErrorModel {
  message: string;
  hint: string;
}

/** What to tell the person when the card cannot load, and what to do about it. */
export function errorModel(kind: ErrorKind, detail: string): ErrorModel {
  switch (kind) {
    case "config":
      return { message: detail, hint: "Set it under Project Settings, Script properties, in the Apps Script editor." };
    case "auth":
      return {
        message: detail,
        hint: "Check that ADDON_OAUTH_CLIENT_ID and ADDON_ALLOWED_EMAIL on the API match this account. Run debugToken in the editor to see the client id.",
      };
    case "network":
      return { message: detail, hint: "Is the API running, and is API_BASE_URL its current address? A tunnel address changes each time it restarts." };
    case "server":
      return { message: detail, hint: "The API answered with an error. Check its logs." };
    case "parse":
      return { message: detail, hint: "The API's response was not what this add-on expects. The two may be out of step." };
  }
}

const KNOWN_ACTIONS = new Set(Object.keys(ACTION_LABELS));

export interface ActionRequest {
  emailId: string;
  action: string;
}

/** Reads a button press's parameters (Apps Script passes them as strings), or null if they are not ours. */
export function parseActionParameters(parameters: unknown): ActionRequest | null {
  if (typeof parameters !== "object" || parameters === null) return null;
  const { emailId, action } = parameters as Record<string, unknown>;
  if (typeof emailId !== "string" || !emailId || typeof action !== "string" || !KNOWN_ACTIONS.has(action)) return null;
  return { emailId, action };
}
