import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { C, fontFamily } from "./theme";

export const TZ = "America/Los_Angeles";
export const fmtDate = (d: Date, opts: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-US", { timeZone: TZ, ...opts }).format(d);

/** Fades and slides in at `from`, out at `to` (frames, relative to the scene). */
export const useAppear = (from: number, to = Infinity, distance = 16) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const inP = spring({ frame: frame - from, fps, config: { damping: 200 }, durationInFrames: 14 });
  const outP = to === Infinity ? 0 : interpolate(frame, [to - 8, to], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
  return { opacity: inP * (1 - outP), transform: `translateY(${(1 - inP) * distance}px)` };
};

/** A caption pill at the bottom of the frame, describing only what is on screen. */
export const Caption: React.FC<{ text: string; from: number; to?: number }> = ({ text, from, to = Infinity }) => {
  const style = useAppear(from, to, 10);
  return (
    <div style={{ position: "absolute", left: 0, right: 0, bottom: 56, display: "flex", justifyContent: "center", ...style }}>
      <div
        style={{
          fontFamily, fontSize: 34, fontWeight: 600, color: "#fff", background: "rgba(17,24,39,.88)",
          padding: "14px 28px", borderRadius: 999, letterSpacing: -0.2,
        }}
      >
        {text}
      </div>
    </div>
  );
};

export const SceneFrame: React.FC<{ children: React.ReactNode; bg?: string }> = ({ children, bg = C.bg }) => (
  <div style={{ position: "absolute", inset: 0, background: bg, fontFamily, color: C.text, overflow: "hidden" }}>{children}</div>
);

export const BrandMark: React.FC<{ size?: number }> = ({ size = 64 }) => (
  <div style={{ width: size, height: size, borderRadius: size * 0.26, background: C.accent, color: "#fff", display: "grid", placeItems: "center" }}>
    <svg width={size * 0.56} height={size * 0.56} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="17" rx="3" />
      <path d="M3 9h18M8 2v4M16 2v4M8.5 14.5l2.5 2.5 4.5-5" />
    </svg>
  </div>
);

export const Badge: React.FC<{ children: React.ReactNode; bg: string; color: string; size?: number }> = ({ children, bg, color, size = 20 }) => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: 6, background: bg, color, fontSize: size, fontWeight: 600, padding: `${size * 0.2}px ${size * 0.6}px`, borderRadius: 999, whiteSpace: "nowrap" }}>
    {children}
  </span>
);

/** A small label naming the technology behind what is on screen (top-right, or bottom-right clear of the caption). */
export const TechTag: React.FC<{ label: string; from?: number; to?: number; corner?: "top" | "bottom" }> = ({ label, from = 4, to = Infinity, corner = "top" }) => {
  const style = useAppear(from, to, corner === "top" ? -8 : 8);
  return (
    <div style={{ position: "absolute", ...(corner === "top" ? { top: 34 } : { bottom: 66 }), right: 40, display: "flex", alignItems: "center", gap: 10, fontFamily, fontSize: 21, fontWeight: 600, color: C.muted, background: "rgba(255,255,255,.92)", border: `1px solid ${C.border}`, borderRadius: 999, padding: "9px 18px", ...style }}>
      <span style={{ width: 9, height: 9, borderRadius: 99, background: C.accent }} />
      {label}
    </div>
  );
};
