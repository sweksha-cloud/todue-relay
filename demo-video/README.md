# Demo video

A 50-second video of ToDue Relay working end to end, built with [Remotion](https://www.remotion.dev)
(React components rendered to MP4). 1920x1080, 30 fps, captions, no audio.

Nothing in it is mocked up by hand. Every email is made up, but what happens to them is real:

- **Pipeline scene** (pre-filter, LLM extraction, confidence gating): drawn from `src/data/trace.json`,
  written by `backend/scripts/demo_video.py run`, which runs the real pipeline (the real pre-filter, the
  real Gemini call, the real routing) over the nine emails in that script.
- **Dashboard scene:** a screen recording of the real FastAPI dashboard over the same demo database.

Gmail and Google Calendar are replaced by stand-ins for the demo, and the scripts refuse any database
whose name doesn't contain `demo`, so no real inbox, calendar or production data is ever touched.

## Rebuild it

```bash
# 1. Run the real pipeline over the fake emails (one Gemini call per email that passes the pre-filter)
cd backend
DEMO_DATABASE_URL=postgresql+psycopg://postgres:<pw>@localhost:5432/demodb python -m scripts.demo_video run

# 2. Serve the real dashboard over a fresh copy of that database (http://localhost:8002), and leave it running
DEMO_DATABASE_URL=postgresql+psycopg://postgres:<pw>@localhost:5432/demodb python -m scripts.demo_video serve

# 3. In another terminal: record the dashboard, then render
cd demo-video
npm install
npm run record      # writes public/dashboard.mp4 and src/data/dashboard-timing.json
npm run render      # writes out/todue-demo.mp4
```

`npm run studio` opens Remotion's preview to scrub through the video while editing. The rendered MP4 and
the recording are gitignored.
