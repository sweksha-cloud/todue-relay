import React from "react";
import { AbsoluteFill, interpolate, Sequence, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { Badge, Caption, SceneFrame, TechTag, useAppear } from "../components";
import { EMAILS, filterReason, NEW_EMAIL, Route, shownExtraction, TraceEmail } from "../data";
import { C, cardStyle, monoFamily, sec } from "../theme";

const STAGE = sec(5);
const STEPS = ["Pre-filter (rules)", "LLM extraction", "Confidence gating"];

const Stepper: React.FC = () => {
  const frame = useCurrentFrame();
  const active = Math.min(2, Math.floor(frame / STAGE));
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

/* Stage 1: every email meets the rules-based pre-filter; the ones with too few signals stop here. */
const FilterStage: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ padding: "132px 220px 150px", display: "flex", flexDirection: "column", gap: 6 }}>
      {EMAILS.map((e, i) => {
        const at = 12 + i * 7;
        const decided = frame >= at;
        const dropped = decided && !e.filter.passed;
        return (
          <div key={e.id} style={{ ...cardStyle, borderRadius: 10, height: 50, display: "flex", alignItems: "center", gap: 20, padding: "0 22px", fontSize: 21, opacity: dropped ? interpolate(frame, [at, at + 10], [1, 0.45], { extrapolateRight: "clamp" }) : 1 }}>
            <div style={{ width: 280, fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{e.sender}</div>
            <div style={{ flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", textDecoration: dropped ? "line-through" : "none", color: dropped ? C.muted : C.text }}>{e.subject}</div>
            <div style={{ width: 420, display: "flex", justifyContent: "flex-end" }}>
              {decided && (e.filter.passed
                ? <Badge bg={C.accentSoft} color={C.accentText}>✓ Sent to the LLM</Badge>
                : <Badge bg={C.redSoft} color={C.redText}>✗ {filterReason(e)}</Badge>)}
            </div>
          </div>
        );
      })}
      <Caption text="Promos and chatter are filtered out before any LLM call" from={sec(1.4)} to={STAGE} />
      <TechTag label="Python · hourly on AWS Lambda" corner="bottom" />
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
  const passed = EMAILS.filter((e) => e.filter.passed);
  const fields = Object.entries(shownExtraction(NEW_EMAIL) ?? {});
  return (
    <AbsoluteFill style={{ padding: "160px 140px 160px", flexDirection: "row", gap: 48 }}>
      <div style={{ width: 620, display: "flex", flexDirection: "column", gap: 14 }}>
        {passed.map((e, i) => (
          <div key={e.id} style={{ ...cardStyle, borderRadius: 12, padding: "16px 22px", fontSize: 23, border: e.id === NEW_EMAIL.id ? `2px solid ${C.accent}` : cardStyle.border, ...useAppear(i * 4) }}>
            <div style={{ fontWeight: 700 }}>{e.subject}</div>
            <div style={{ color: C.muted, fontSize: 20, marginTop: 4 }}>{e.sender}</div>
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
  const passed = EMAILS.filter((e) => e.filter.passed);
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
    <Sequence durationInFrames={STAGE}><FilterStage /></Sequence>
    <Sequence from={STAGE} durationInFrames={STAGE}><ExtractStage /></Sequence>
    <Sequence from={STAGE * 2} durationInFrames={STAGE}><GateStage /></Sequence>
  </SceneFrame>
);
