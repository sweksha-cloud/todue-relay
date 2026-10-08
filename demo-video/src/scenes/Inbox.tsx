import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { Caption, SceneFrame, TechTag } from "../components";
import { INBOX_REST, NEW_EMAIL, TraceEmail } from "../data";
import { C, cardStyle, sec } from "../theme";

const ROW_H = 55;
const ARRIVE = sec(2.2);

// Shown newest first, with the emails that matter mixed in among the everyday ones, as in a real inbox.
const ORDER = ["demo-10", "demo-02", "demo-03", "demo-11", "demo-05", "demo-04", "demo-13", "demo-07", "demo-12", "demo-06", "demo-09", "demo-14", "demo-08"];
const SHOWN = ORDER.map((id) => INBOX_REST.find((e) => e.id === id)).filter((e): e is TraceEmail => Boolean(e));
const TIMES = ["9:12 AM", "9:05 AM", "8:47 AM", "8:41 AM", "8:30 AM", "8:22 AM", "8:02 AM", "7:58 AM", "7:41 AM", "7:30 AM", "Oct 6", "Oct 6", "Oct 6"];
const UNREAD = 1284;

const Row: React.FC<{ e: TraceEmail; time: string; unread: boolean; highlight?: number }> = ({ e, time, unread, highlight = 0 }) => (
  <div
    style={{
      height: ROW_H, display: "flex", alignItems: "center", gap: 22, padding: "0 26px",
      borderBottom: `1px solid ${C.border}`, fontSize: 22,
      background: highlight ? `rgba(37,99,235,${0.08 * highlight})` : unread ? C.card : "#fafbfc",
    }}
  >
    <div style={{ width: 22, height: 22, border: `2px solid ${C.borderStrong}`, borderRadius: 5, flexShrink: 0 }} />
    <div style={{ width: 300, flexShrink: 0, fontWeight: unread ? 700 : 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{e.sender}</div>
    <div style={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
      <span style={{ fontWeight: unread ? 700 : 500 }}>{e.subject}</span>
      <span style={{ color: C.muted }}> — {e.snippet}</span>
    </div>
    <div style={{ width: 110, textAlign: "right", flexShrink: 0, fontWeight: unread ? 700 : 400, color: unread ? C.text : C.muted, fontSize: 22 }}>{time}</div>
  </div>
);

export const Inbox: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const arrive = spring({ frame: frame - ARRIVE, fps, config: { damping: 18, stiffness: 120 } });
  const glow = interpolate(frame, [ARRIVE + 10, ARRIVE + 25, sec(7.4)], [0, 1, 0.6], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  const zoom = interpolate(frame, [0, sec(8)], [1, 1.035]);

  return (
    <SceneFrame>
      <div style={{ position: "absolute", inset: "56px 80px 150px", display: "flex", gap: 28, transform: `scale(${zoom})` }}>
        {/* Sidebar */}
        <div style={{ width: 250, display: "flex", flexDirection: "column", gap: 8, paddingTop: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14, fontSize: 32, fontWeight: 700, marginBottom: 22 }}>
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke={C.text} strokeWidth={2}><rect x="2" y="4" width="20" height="16" rx="2" /><path d="m22 7-10 6L2 7" /></svg>
            Mail
          </div>
          {["Inbox", "Starred", "Sent", "Drafts"].map((l, i) => (
            <div key={l} style={{ fontSize: 25, padding: "12px 18px", borderRadius: 999, background: i === 0 ? C.accentSoft : "transparent", color: i === 0 ? C.accentText : C.muted, fontWeight: i === 0 ? 700 : 500, display: "flex", justifyContent: "space-between" }}>
              {l}
              {i === 0 && <span>{(frame >= ARRIVE ? UNREAD + 1 : UNREAD).toLocaleString("en-US")}</span>}
            </div>
          ))}
        </div>
        {/* List */}
        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 18 }}>
          <div style={{ height: 64, borderRadius: 999, background: C.subtle, display: "flex", alignItems: "center", padding: "0 28px", color: C.muted, fontSize: 24 }}>Search mail</div>
          <div style={{ ...cardStyle, overflow: "hidden", flex: 1 }}>
            <div style={{ height: arrive * ROW_H, overflow: "hidden" }}>
              <div style={{ transform: `translateY(${(arrive - 1) * ROW_H}px)` }}>
                <Row e={NEW_EMAIL} time="9:14 AM" unread highlight={glow} />
              </div>
            </div>
            {SHOWN.map((e, i) => (
              <Row key={e.id} e={e} time={TIMES[i]} unread />
            ))}
          </div>
        </div>
      </div>
      <Caption text="Too much mail: the important things get buried" from={6} to={ARRIVE + 6} />
      <Caption text="New email arrives" from={ARRIVE + 10} />
      <TechTag label="Read through the Gmail API" />
    </SceneFrame>
  );
};
