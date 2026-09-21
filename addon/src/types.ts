// The shape of GET /api/addon/summary. It mirrors the Summary model in backend/app/addon_api.py; a
// change there must change this. (Generating it from the API's schema is the natural next step.)

export interface EmailView {
  email_id: string;
  subject: string;
  event_name: string | null;
  status: string; // added | needs review | skipped | processing | failed
  confidence: string | null;
  action_type: string | null;
  deadline_iso: string | null;
  deadline_text: string | null;
  on_calendar: boolean;
  is_implausible_date: boolean;
  vote: "correct" | "incorrect" | null;
}

export interface RunView {
  status: string; // running | success | failure
  started_text: string;
  fetched: number;
  processed: number;
  failed: number;
}

export interface Summary {
  counts: { needs_review: number; upcoming: number; action_items: number };
  needs_review: EmailView[];
  upcoming: EmailView[];
  action_items: EmailView[];
  latest_run: RunView | null;
}
