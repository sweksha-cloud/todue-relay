import React from "react";
import { OffthreadVideo, staticFile } from "remotion";
import { Caption, SceneFrame, TechTag } from "../components";
import { C, sec } from "../theme";
import timing from "../data/dashboard-timing.json";

// public/dashboard.mp4 is the real FastAPI dashboard, recorded by scripts/record-dashboard.mjs against the
// demo database (never production). dashboard-timing.json holds when each step happened in that recording.
const t = timing as { approveClickAt: number; calendarShownAt: number; durationSec: number };

export const Dashboard: React.FC = () => (
  <SceneFrame bg="#e9ecf1">
    <div style={{ position: "absolute", left: 150, right: 150, top: 40, bottom: 140, borderRadius: 14, overflow: "hidden", boxShadow: "0 20px 60px rgba(16,24,40,.25)", background: C.card, display: "flex", flexDirection: "column" }}>
      <div style={{ height: 44, background: "#eef0f3", display: "flex", alignItems: "center", gap: 9, padding: "0 18px", borderBottom: `1px solid ${C.border}` }}>
        {["#ff5f57", "#febc2e", "#28c840"].map((c) => <span key={c} style={{ width: 13, height: 13, borderRadius: 99, background: c }} />)}
        <div style={{ marginLeft: 18, flex: 1, maxWidth: 520, height: 28, borderRadius: 8, background: "#fff", fontSize: 16, color: C.muted, display: "flex", alignItems: "center", padding: "0 12px" }}>localhost:8000</div>
      </div>
      <OffthreadVideo src={staticFile("dashboard.mp4")} style={{ width: "100%", flex: 1, objectFit: "cover", objectPosition: "top" }} muted />
    </div>
    <Caption text="The real dashboard: low confidence, so it waits for review" from={sec(0.5)} to={sec(t.approveClickAt) - 4} />
    <Caption text="Approved" from={sec(t.approveClickAt)} to={sec(t.calendarShownAt) - 4} />
    <Caption text="Now on the calendar" from={sec(t.calendarShownAt)} />
    <TechTag label="FastAPI · htmx · PostgreSQL" />
  </SceneFrame>
);
