import { loadFont } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

export const { fontFamily } = loadFont("normal", { weights: ["400", "500", "600", "700"], subsets: ["latin"] });
export const { fontFamily: monoFamily } = loadMono("normal", { weights: ["400", "500"], subsets: ["latin"] });

// The same palette as the dashboard (backend/app/templates/base.html, light mode).
export const C = {
  bg: "#f6f7f9",
  card: "#ffffff",
  subtle: "#f1f2f5",
  border: "#e5e7eb",
  borderStrong: "#d1d5db",
  text: "#111827",
  muted: "#6b7280",
  accent: "#2563eb",
  accentSoft: "#eff4ff",
  accentText: "#1d4ed8",
  green: "#16a34a",
  greenSoft: "#ecfdf3",
  greenText: "#15803d",
  amberSoft: "#fff7e6",
  amberText: "#b45309",
  redSoft: "#fef2f2",
  redText: "#b91c1c",
  violetSoft: "#f3efff",
  violetText: "#6d28d9",
};

export const FPS = 30;
export const sec = (s: number) => Math.round(s * FPS);

export const cardStyle: React.CSSProperties = {
  background: C.card,
  border: `1px solid ${C.border}`,
  borderRadius: 16,
  boxShadow: "0 1px 2px rgba(16,24,40,.04), 0 4px 12px rgba(16,24,40,.06)",
};
