import type { EmailView, Summary } from "../src/types";

export function email(overrides: Partial<EmailView> = {}): EmailView {
  return {
    email_id: "e1",
    subject: "Nominations due Sept 24",
    event_name: "Nominations due",
    status: "added",
    confidence: "high",
    action_type: "deadline",
    deadline_iso: "2026-09-24T17:00:00-07:00",
    deadline_text: "Thu Sep 24, 5:00 PM",
    on_calendar: true,
    is_implausible_date: false,
    vote: null,
    ...overrides,
  };
}

export function summary(overrides: Partial<Summary> = {}): Summary {
  return {
    counts: { needs_review: 0, upcoming: 0, action_items: 0 },
    needs_review: [],
    upcoming: [],
    action_items: [],
    latest_run: null,
    ...overrides,
  };
}
