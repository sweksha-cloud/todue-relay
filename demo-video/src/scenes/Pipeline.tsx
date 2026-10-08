import React from "react";
import { AbsoluteFill, interpolate, Sequence, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { Badge, Caption, SceneFrame, TechTag, useAppear } from "../components";
import { EMAILS, NEW_EMAIL, PASSED, Route, shownExtraction, TraceEmail } from "../data";
import { C, cardStyle, monoFamily, sec } from "../theme";

// Stage lengths. The pre-filter gets longest: it is a 17-row rule table plus its summary.
const FILTER = sec(8.5); // long enough to read the summary: 17 emails -> 8 LLM calls
const EXTRACT = sec(5);
const GATE = sec(5);
export const PIPELINE_LENGTH = FILTER + EXTRACT + GATE;
const STEPS = ["Pre-filter (rules)", "LLM extraction", "Confidence gating"];

const Stepper: React.FC = () => {
  const frame = useCurrentFrame();
  const active = frame < FILTER ? 0 : frame < FILTER + EXTRACT ? 1 : 2;
  return (
    <div style={{ position: "absolute", top: 54, left: 0, right: 0, display: "flex", justifyContent: "center", gap: 18, alignItems: "center" }}>
      {STEPS.map((s, i) => (
        <React.Fragment key={s}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, fontSize: 30, fontWeight: 600, color: i === active ? C.text : C.muted, opacity: i <= active ? 1 : 0.55 }}>
            <span style={{ width: 46, height: 46, borderRadius: 999, display: "grid", placeItems: "center", fontSize: 24, background: i < active ? C.green : i === active ? C.accent : C.subtle, color: i <= active ? "#fff" : C.muted }}>
              {i < active ? "✓" : i + 1}
            </span>
            {s}
          </div>
          {i < 2 && <div style={{ width: 70, height: 3, background: i < active ? C.green : C.border, borderRadius: 2 }} />}
        </React.Fragment>
      ))}
    </div>
  );
};

/* Stage 1: the rules-based pre-filter. Each email is scored against three regex rule sets (backend/app/filters.py);
   it reaches the LLM only if at least 2 of the 3 match. Every token shown is what the real regexes matched. */
const RULES: { key: "keyword" | "action_verb" | "date_pattern"; label: string }[] = [
  { key: "keyword", label: "deadline words" },
  { key: "action_verb", label: "action words" },
  { key: "date_pattern", label: "date" },
];
const COLS = "minmax(0, 1fr) 210px 210px 210px 90px 150px";
const ROW_STAGGER = 5;

const Token: React.FC<{ text: string | null }> = ({ text }) =>
  text ? (
    <span style={{ fontFamily: monoFamily, fontSize: 18, color: C.greenText, background: C.greenSoft, padding: "3px 9px", borderRadius: 6, whiteSpace: "nowrap" }}>"{text}"</span>
  ) : (
    <span style={{ fontFamily: monoFamily, fontSize: 18, color: C.borderStrong }}>—</span>
  );

const FilterStage: React.FC = () => {
  const frame = useCurrentFrame();
  const decidedCount = EMAILS.filter((_, i) => frame >= 14 + i * ROW_STAGGER).length;
  const calls = EMAILS.slice(0, decidedCount).filter((e) => e.filter.passed).length;
  const done = 14 + EMAILS.length * ROW_STAGGER + 6;
  const summary = useAppear(done);
  return (
    <AbsoluteFill style={{ padding: "112px 150px 0" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <div style={{ fontFamily: monoFamily, fontSize: 20, color: C.muted }}>
          pass if <span style={{ color: C.text }}>score ≥ 2 of 3</span> rule sets match · FILTER_LEVEL=moderate
        </div>
        <div style={{ fontFamily: monoFamily, fontSize: 22, color: C.text }}>
          LLM calls: <span style={{ color: C.accentText, fontWeight: 600 }}>{calls}</span> / {decidedCount}
        </div>
      </div>
      <div style={{ ...cardStyle, borderRadius: 12, overflow: "hidden" }}>
        <div style={{ display: "grid", gridTemplateColumns: COLS, alignItems: "center", gap: 16, padding: "10px 20px", background: C.subtle, fontSize: 16, fontWeight: 700, color: C.muted, letterSpacing: 0.6, textTransform: "uppercase" }}>
          <div>Email</div>
          {RULES.map((r) => <div key={r.key}>{r.label}</div>)}
          <div>Score</div>
          <div>Result</div>
        </div>
        {EMAILS.map((e, i) => {
          const at = 14 + i * ROW_STAGGER;
          const shown = frame >= at;
          const score = RULES.filter((r) => e.filter.signals[r.key]).length;
          const dim = shown && !e.filter.passed;
          return (
            <div key={e.id} style={{ display: "grid", gridTemplateColumns: COLS, alignItems: "center", gap: 16, height: 40, padding: "0 20px", borderTop: `1px solid ${C.border}`, fontSize: 19, background: shown && e.filter.passed ? "#f8faff" : C.card }}>
              <div style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", color: dim ? C.muted : C.text, fontWeight: shown && e.filter.passed ? 600 : 400 }}>{e.subject}</div>
              {RULES.map((r) => <div key={r.key} style={{ opacity: shown ? 1 : 0 }}><Token text={e.filter.matches[r.key]} /></div>)}
              <div style={{ fontFamily: monoFamily, fontSize: 19, opacity: shown ? 1 : 0, color: score >= 2 ? C.text : C.muted }}>{score}/3</div>
              <div style={{ opacity: shown ? 1 : 0 }}>
                {e.filter.passed
                  ? <Badge bg={C.accentSoft} color={C.accentText} size={17}>→ LLM</Badge>
                  : <Badge bg={C.subtle} color={C.muted} size={17}>skipped</Badge>}
              </div>
            </div>
          );
        })}
      </div>
      <Caption text={`Rules-based pre-filter: ${EMAILS.length} emails → ${PASSED.length} LLM calls`} from={done} to={FILTER} />
      <div style={{ position: "absolute", left: 0, right: 0, bottom: 136, display: "flex", justifyContent: "center", ...summary }}>
        <div style={{ fontFamily: monoFamily, fontSize: 21, color: C.muted }}>
          {EMAILS.length - PASSED.length} of {EMAILS.length} emails never reach the LLM: no call, no quota spent
        </div>
      </div>
      <TechTag label="Python regex · AWS Lambda" corner="bottom" />
    </AbsoluteFill>
  );
};

/* Stage 2: the JSON Gemini actually returned for the new email, typed out field by field. */
const JsonLine: React.FC<{ k: string; v: unknown; at: number; last: boolean }> = ({ k, v, at, last }) => {
  const style = useAppear(at, Infinity, 8);
  const color = typeof v === "string" ? "#0f766e" : v === null ? C.muted : "#b45309";
  return (
    <div style={style}>
      <span style={{ color: "#6d28d9" }}>"{k}"</span>: <span style={{ color }}>{JSON.stringify(v)}</span>{last ? "" : ","}
    </div>
  );
};

const ExtractStage: React.FC = () => {
  const passed = PASSED;
  const fields = Object.entries(shownExtraction(NEW_EMAIL) ?? {});
  return (
    <AbsoluteFill style={{ padding: "160px 140px 160px", flexDirection: "row", gap: 48 }}>
      <div style={{ width: 620, display: "flex", flexDirection: "column", gap: 10 }}>
        {passed.map((e, i) => (
          <div key={e.id} style={{ ...cardStyle, borderRadius: 12, padding: "11px 20px", fontSize: 21, border: e.id === NEW_EMAIL.id ? `2px solid ${C.accent}` : cardStyle.border, ...useAppear(i * 4) }}>
            <div style={{ fontWeight: 700 }}>{e.subject}</div>
            <div style={{ color: C.muted, fontSize: 18, marginTop: 2 }}>{e.sender}</div>
          </div>
        ))}
      </div>
      <div style={{ flex: 1, ...cardStyle, background: "#0f172a", border: "none", padding: "30px 38px", fontFamily: monoFamily, fontSize: 25, lineHeight: 1.6, color: "#e2e8f0", ...useAppear(8) }}>
        <div style={{ color: "#94a3b8", fontSize: 20, marginBottom: 10, fontFamily: "inherit" }}>Gemini response · {NEW_EMAIL.subject}</div>
        <div>{"{"}</div>
        <div style={{ paddingLeft: 32 }}>
          {fields.map(([k, v], i) => <JsonLine key={k} k={k} v={v} at={18 + i * 8} last={i === fields.length - 1} />)}
        </div>
        <div>{"}"}</div>
      </div>
      <Caption text="The LLM returns structured, schema-validated JSON" from={sec(0.6)} />
      <TechTag label="Gemini API · Pydantic" corner="bottom" />
    </AbsoluteFill>
  );
};

/* Stage 3: where the real pipeline routed each email that reached the LLM. */
const COLUMNS: { route: Route; title: string; bg: string; color: string }[] = [
  { route: "auto_scheduled", title: "Auto-scheduled", bg: C.greenSoft, color: C.greenText },
  { route: "needs_review", title: "Needs review", bg: C.amberSoft, color: C.amberText },
  { route: "action_item", title: "Action item", bg: C.violetSoft, color: C.violetText },
];

const RoutedCard: React.FC<{ e: TraceEmail; at: number }> = ({ e, at }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const p = spring({ frame: frame - at, fps, config: { damping: 16 } });
  const conf = (e.extraction?.confidence as string) ?? "";
  return (
    <div style={{ ...cardStyle, borderRadius: 12, padding: "18px 22px", fontSize: 23, opacity: p, transform: `translateY(${(1 - p) * 40}px) scale(${0.95 + p * 0.05})` }}>
      <div style={{ fontWeight: 700 }}>{(e.extraction?.event_name as string) ?? e.subject}</div>
      <div style={{ color: C.muted, fontSize: 20, marginTop: 6 }}>{conf ? `${conf} confidence` : ""}{e.extraction?.deadline_date ? ` · ${e.extraction.deadline_date}` : ""}</div>
    </div>
  );
};

const GateStage: React.FC = () => {
  const passed = PASSED;
  let order = 0;
  return (
    <AbsoluteFill style={{ padding: "170px 110px 170px", flexDirection: "row", gap: 32 }}>
      {COLUMNS.map((col) => (
        <div key={col.route} style={{ flex: 1, display: "flex", flexDirection: "column", gap: 16 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "14px 0", borderRadius: 12, background: col.bg, color: col.color, fontSize: 28, fontWeight: 700 }}>{col.title}</div>
          {passed.filter((e) => e.route === col.route).map((e) => <RoutedCard key={e.id} e={e} at={12 + 10 * order++} />)}
        </div>
      ))}
      <Caption text="High confidence goes to the calendar; low confidence goes to review" from={sec(1)} />
      <TechTag label="Recorded in PostgreSQL" corner="bottom" />
    </AbsoluteFill>
  );
};

export const Pipeline: React.FC = () => (
  <SceneFrame>
    <Stepper />
    <Sequence durationInFrames={FILTER}><FilterStage /></Sequence>
    <Sequence from={FILTER} durationInFrames={EXTRACT}><ExtractStage /></Sequence>
    <Sequence from={FILTER + EXTRACT} durationInFrames={GATE}><GateStage /></Sequence>
  </SceneFrame>
);
