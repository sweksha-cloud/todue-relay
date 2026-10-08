// Records the real dashboard for the video's review scene, against the demo database only.
//
//   1. cd backend && DEMO_DATABASE_URL=... python -m scripts.demo_video serve   (http://localhost:8002)
//   2. cd demo-video && npm run record
//
// Writes public/dashboard.mp4 and src/data/dashboard-timing.json (when Approve was clicked and when the
// approved item showed up on the calendar, in seconds into the clip). Uses the system Chrome.
import { chromium } from "playwright-core";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const DASHBOARD_URL = process.env.DEMO_DASHBOARD_URL ?? "http://localhost:8002/";
const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SIZE = { width: 1620, height: 900 }; // the browser frame in the video is 1620 wide
const here = new URL("..", import.meta.url).pathname;

// A visible cursor: Playwright's recording does not draw the real one.
const CURSOR = `
  addEventListener("DOMContentLoaded", () => {
    document.documentElement.style.zoom = "1.35"; // the dashboard is 880px wide; fill more of the frame
    const c = document.createElement("div");
    c.style.cssText = "position:fixed;z-index:99999;width:22px;height:22px;border-radius:50%;pointer-events:none;" +
      "background:rgba(37,99,235,.35);border:2px solid #2563eb;transform:translate(-50%,-50%);left:-50px;top:-50px;transition:transform .12s";
    document.body.appendChild(c);
    addEventListener("mousemove", (e) => { c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px"; });
    addEventListener("mousedown", () => (c.style.transform = "translate(-50%,-50%) scale(.7)"));
    addEventListener("mouseup", () => (c.style.transform = "translate(-50%,-50%)"));
  });`;

const dir = mkdtempSync(join(tmpdir(), "todue-rec-"));
const browser = await chromium.launch({ executablePath: CHROME });
const context = await browser.newContext({ viewport: SIZE, colorScheme: "light", recordVideo: { dir, size: SIZE } });
await context.addInitScript(CURSOR);
const page = await context.newPage();
const t0 = Date.now();
const at = () => (Date.now() - t0) / 1000;

await page.goto(DASHBOARD_URL);
await page.waitForLoadState("networkidle");
const start = at(); // everything before this is a blank page loading; trimmed off below
await page.mouse.move(800, 300);
await page.waitForTimeout(1500); // time to see the held-back item

// The held-back email in "Needs your review".
const row = page.locator("#section-needs_review .item").first();
const title = (await row.locator(".item-title").innerText()).trim();
const approve = row.getByRole("button", { name: "Approve" });
let approveClickAt;
if (await approve.count()) {
  const box = await approve.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 12 });
  await page.waitForTimeout(350);
  approveClickAt = at();
  await Promise.all([page.waitForEvent("load"), page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)]);
} else {
  // No date was found, so it is added at a chosen time instead (the Reschedule dropdown).
  const btn = row.getByRole("button", { name: "Reschedule" });
  const box = await btn.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 25 });
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(500);
  await row.locator('input[type="datetime-local"]').fill("2026-10-30T17:00");
  await page.waitForTimeout(600);
  const add = row.getByRole("button", { name: "Add to calendar" });
  const abox = await add.boundingBox();
  await page.mouse.move(abox.x + abox.width / 2, abox.y + abox.height / 2, { steps: 12 });
  approveClickAt = at();
  await Promise.all([page.waitForEvent("load"), page.mouse.click(abox.x + abox.width / 2, abox.y + abox.height / 2)]);
}
await page.waitForLoadState("networkidle");
await page.waitForTimeout(400);

// Scroll to where it now sits, in "On your calendar", and point at it.
const landed = page.locator("#section-to_check .item", { hasText: title }).first();
await landed.evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "center" }));
await page.waitForTimeout(900);
await page.mouse.move(1500, 120, { steps: 20 }); // out of the way: the highlight marks the item
const calendarShownAt = at();
await landed.evaluate((el) => { el.style.transition = "background .4s"; el.style.background = "#ecfdf3"; el.style.borderRadius = "10px"; });
await page.waitForTimeout(2200);
const end = at();

await context.close();
await browser.close();

const webm = join(dir, readdirSync(dir).find((f) => f.endsWith(".webm")));
const out = join(here, "public", "dashboard.mp4");
execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-ss", String(start), "-i", webm, "-t", String(end - start),
  "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30", out]);
const timing = {
  approveClickAt: +(approveClickAt - start).toFixed(2),
  calendarShownAt: +(calendarShownAt - start).toFixed(2),
  durationSec: +(end - start).toFixed(2),
  item: title,
};
writeFileSync(join(here, "src", "data", "dashboard-timing.json"), JSON.stringify(timing, null, 2) + "\n");
console.log("recorded", out, timing);
