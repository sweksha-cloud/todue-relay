import React from "react";
import { linearTiming, TransitionSeries } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import { slide } from "@remotion/transitions/slide";
import { TitleCard } from "./scenes/TitleCard";
import { Inbox } from "./scenes/Inbox";
import { Pipeline } from "./scenes/Pipeline";
import { Calendar } from "./scenes/Calendar";
import { Dashboard } from "./scenes/Dashboard";
import { sec } from "./theme";
import timing from "./data/dashboard-timing.json";

export const TRANSITION = 12;
export const SCENES = {
  title: sec(3),
  inbox: sec(8),
  pipeline: sec(15),
  calendar: sec(9),
  dashboard: Math.min(sec(15), sec(timing.durationSec)), // as long as the recording, capped at 15 s
  end: sec(4),
};
// Five transitions overlap their neighbours, so they shorten the total.
export const TOTAL = Object.values(SCENES).reduce((a, b) => a + b, 0) - 5 * TRANSITION;

// An element, not a component: TransitionSeries only accepts its own element types as children.
const T = <TransitionSeries.Transition presentation={fade()} timing={linearTiming({ durationInFrames: TRANSITION })} />;

export const TodueDemo: React.FC = () => (
  <TransitionSeries>
    <TransitionSeries.Sequence durationInFrames={SCENES.title}>
      <TitleCard tagline="Deadlines buried in your inbox, auto-scheduled to your calendar." />
    </TransitionSeries.Sequence>
    {T}
    <TransitionSeries.Sequence durationInFrames={SCENES.inbox}><Inbox /></TransitionSeries.Sequence>
    <TransitionSeries.Transition presentation={slide({ direction: "from-right" })} timing={linearTiming({ durationInFrames: TRANSITION })} />
    <TransitionSeries.Sequence durationInFrames={SCENES.pipeline}><Pipeline /></TransitionSeries.Sequence>
    {T}
    <TransitionSeries.Sequence durationInFrames={SCENES.calendar}><Calendar /></TransitionSeries.Sequence>
    {T}
    <TransitionSeries.Sequence durationInFrames={SCENES.dashboard}><Dashboard /></TransitionSeries.Sequence>
    {T}
    <TransitionSeries.Sequence durationInFrames={SCENES.end}>
      <TitleCard stack="Python · FastAPI · PostgreSQL · Gemini · AWS Lambda · TypeScript" footer="github.com/sweksha-cloud/todue-relay" />
    </TransitionSeries.Sequence>
  </TransitionSeries>
);
