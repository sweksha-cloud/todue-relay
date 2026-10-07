import React from "react";
import { spring, useCurrentFrame, useVideoConfig } from "remotion";
import { Caption, fmtDate, SceneFrame, TechTag } from "../components";
import { deadlineDate, EMAILS, TraceEmail } from "../data";
import { C, cardStyle, sec } from "../theme";

const YEAR = 2026;
const MONTH = 9; // October (0-based)
const TODAY = 7;

const scheduled = EMAILS.filter((e) => e.route === "auto_scheduled" && e.deadline);
const dayOf = (e: TraceEmail) => Number(fmtDate(deadlineDate(e)!, { day: "numeric" }));

const Chip: React.FC<{ e: TraceEmail; at: number }> = ({ e, at }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const p = spring({ frame: frame - at, fps, config: { damping: 12, stiffness: 140 } });
  const time = e.has_time ? fmtDate(deadlineDate(e)!, { hour: "numeric", minute: "2-digit" }) : "All day";
  return (
    <div style={{ background: C.accent, color: "#fff", borderRadius: 8, padding: "6px 10px", fontSize: 18, fontWeight: 600, lineHeight: 1.25, transform: `scale(${p})`, transformOrigin: "left center", opacity: Math.min(1, p * 1.5) }}>
      <div style={{ fontSize: 15, opacity: 0.85 }}>{time}</div>
      {(e.extraction?.event_name as string) ?? e.subject}
    </div>
  );
};

export const Calendar: React.FC = () => {
  const first = new Date(YEAR, MONTH, 1).getDay();
  const days = new Date(YEAR, MONTH + 1, 0).getDate();
  const cells = Array.from({ length: 35 }, (_, i) => i - first + 1);
  return (
    <SceneFrame>
      <div style={{ position: "absolute", inset: "50px 140px 150px", ...cardStyle, padding: "26px 30px", display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 18, marginBottom: 18 }}>
          <div style={{ fontSize: 40, fontWeight: 700 }}>October 2026</div>
          <div style={{ fontSize: 24, color: C.muted }}>Calendar</div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", fontSize: 20, color: C.muted, fontWeight: 600, paddingBottom: 8 }}>
          {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d) => <div key={d} style={{ paddingLeft: 10 }}>{d}</div>)}
        </div>
        <div style={{ flex: 1, display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gridTemplateRows: "repeat(5, 1fr)", borderTop: `1px solid ${C.border}`, borderLeft: `1px solid ${C.border}` }}>
          {cells.map((d, i) => (
            <div key={i} style={{ borderRight: `1px solid ${C.border}`, borderBottom: `1px solid ${C.border}`, padding: 8, display: "flex", flexDirection: "column", gap: 6, background: d < 1 || d > days ? "#fafbfc" : C.card }}>
              {d >= 1 && d <= days && (
                <div style={{ fontSize: 20, fontWeight: 600, width: 36, height: 36, borderRadius: 999, display: "grid", placeItems: "center", background: d === TODAY ? C.accent : "transparent", color: d === TODAY ? "#fff" : C.text }}>{d}</div>
              )}
              {scheduled.map((e, k) => (dayOf(e) === d ? <Chip key={e.id} e={e} at={sec(1) + k * 14} /> : null))}
            </div>
          ))}
        </div>
      </div>
      <Caption text="Auto-scheduled events land on the calendar, on their dates" from={sec(0.8)} />
      <TechTag label="Written through the Google Calendar API" />
    </SceneFrame>
  );
};
