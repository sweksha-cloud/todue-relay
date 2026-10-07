import React from "react";
import { Composition } from "remotion";
import { TodueDemo, TOTAL } from "./Video";
import { FPS } from "./theme";

export const Root: React.FC = () => (
  <Composition id="TodueDemo" component={TodueDemo} durationInFrames={TOTAL} fps={FPS} width={1920} height={1080} />
);
