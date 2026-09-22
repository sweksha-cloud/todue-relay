// Everything that DECIDES how the home card reads lives here as plain functions of plain data, so
// it can be unit tested. Apps Script is a poor place to debug: the card can only be seen in Gmail.
// cards.ts turns this model into Google's card widgets and holds no decisions of its own.

import type { EmailView, RunView, Summary } from "./types";

export const ACTION_LABELS: Record<string, string> = {
  approve: "Add to calendar",
  approve_at: "Reschedule",
  decline: "Don't add",
  vote_correct: "Correct",
  vote_incorrect: "Incorrect",
  remove: "Remove",
  reschedule: "Reschedule",
  schedule: "Schedule",
};

// These three need a date/time the person types in before the button means anything — see
// dateFieldName below. Every other action fires as soon as its button is pressed.
export const ACTIONS_NEEDING_DATETIME = new Set(["approve_at", "reschedule", "schedule"]);

/** The field name of the date/time TextInput cards.ts draws for one item, so a card with several
 * items needing a date each get their own independent field instead of colliding on one. */
export function dateFieldName(emailId: string): string {
  return `new_datetime__${emailId}`;
}

export interface ItemButton {
  label: string;
  emailId: string;
  action: string;
}

export interface HomeItem {
  emailId: string;
  title: string;
  subtitle: string | null;
  tag: string | null;
  buttons: ItemButton[];
  needsDatetime: boolean;
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
  const needsDatetime = buttons.some((b) => ACTIONS_NEEDING_DATETIME.has(b.action));
  return { emailId: e.email_id, title, subtitle: e.deadline_text, tag, buttons, needsDatetime };
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

export type ActionRequest =
  | { ok: true; emailId: string; action: string; newDatetime?: string }
  | { ok: false; reason: "unrecognised" | "missing_datetime" };

/** Reads a button press's parameters (Apps Script passes them as strings) plus, for the three
 * actions that need one, the date/time typed into that item's own field (formInput carries the
 * current value of every TextInput on the card, keyed by field name — see dateFieldName). */
export function parseActionParameters(parameters: unknown, formInput?: Record<string, unknown>): ActionRequest {
  if (typeof parameters !== "object" || parameters === null) return { ok: false, reason: "unrecognised" };
  const { emailId, action } = parameters as Record<string, unknown>;
  if (typeof emailId !== "string" || !emailId || typeof action !== "string" || !KNOWN_ACTIONS.has(action)) {
    return { ok: false, reason: "unrecognised" };
  }
  if (!ACTIONS_NEEDING_DATETIME.has(action)) return { ok: true, emailId, action };

  const raw = formInput?.[dateFieldName(emailId)];
  if (typeof raw !== "string" || !raw.trim()) return { ok: false, reason: "missing_datetime" };
  return { ok: true, emailId, action, newDatetime: raw.trim() };
}
