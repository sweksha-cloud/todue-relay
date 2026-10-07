// Everything the pipeline scene shows comes from trace.json, written by `python -m scripts.demo_video run`:
// the real pre-filter, the JSON Gemini actually returned, and where the real pipeline routed each email.
import trace from "./data/trace.json";

export type Route = "filtered_out" | "auto_scheduled" | "needs_review" | "action_item" | "skipped" | "failed";

export type TraceEmail = {
  id: string;
  sender: string;
  subject: string;
  snippet: string;
  filter: { signals: { keyword: boolean; action_verb: boolean; date_pattern: boolean }; passed: boolean };
  extraction: Record<string, unknown> | null;
  route: Route;
  deadline: string | null;
  has_time: boolean;
};

export const EMAILS = (trace as { emails: TraceEmail[] }).emails;

// The first email is the one that "arrives" during the inbox scene.
export const NEW_EMAIL = EMAILS[0];
export const INBOX_REST = EMAILS.slice(1);

/** Why the pre-filter dropped an email, in words, from the signals it did not find. */
export const filterReason = (e: TraceEmail): string => {
  const missing = [];
  if (!e.filter.signals.keyword) missing.push("deadline words");
  if (!e.filter.signals.action_verb) missing.push("action words");
  if (!e.filter.signals.date_pattern) missing.push("date");
  if (!missing.length) return "Too few signals";
  const last = missing.pop();
  return `No ${missing.length ? `${missing.join(", ")} or ${last}` : last}`;
};

/** The extraction as shown on screen: the fields Gemini returned, minus the id echo. */
export const shownExtraction = (e: TraceEmail) => {
  if (!e.extraction) return null;
  const { email_id: _id, ...rest } = e.extraction as Record<string, unknown>;
  return rest;
};

export const deadlineDate = (e: TraceEmail) => (e.deadline ? new Date(e.deadline) : null);
