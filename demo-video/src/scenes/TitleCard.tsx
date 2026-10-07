import React from "react";
import { BrandMark, SceneFrame, useAppear } from "../components";
import { C } from "../theme";

export const TitleCard: React.FC<{ tagline?: string; footer?: string; stack?: string }> = ({ tagline, footer, stack }) => {
  const mark = useAppear(0, Infinity, 24);
  const title = useAppear(4, Infinity, 24);
  const sub = useAppear(12, Infinity, 16);
  return (
    <SceneFrame>
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 28 }}>
        <div style={mark}><BrandMark size={104} /></div>
        <div style={{ fontSize: 112, fontWeight: 700, letterSpacing: -3, ...title }}>ToDue Relay</div>
        {tagline && <div style={{ fontSize: 40, color: C.muted, maxWidth: 1300, textAlign: "center", lineHeight: 1.3, ...sub }}>{tagline}</div>}
        {stack && <div style={{ fontSize: 32, color: C.text, fontWeight: 500, ...sub }}>{stack}</div>}
        {footer && <div style={{ fontSize: 34, color: C.accentText, fontWeight: 500, ...sub }}>{footer}</div>}
      </div>
    </SceneFrame>
  );
};
