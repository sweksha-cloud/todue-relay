// Bundles src/ into one plain script for Apps Script, which has no module system: every function
// it should call by name (the manifest's triggers) is attached to the global object in src/main.ts.
import { build } from "esbuild";
import { cpSync, mkdirSync } from "node:fs";

// The bundle above is one big IIFE, so onHomepage/onRefresh/onAction/debugToken only exist as
// closures assigned onto globalThis *inside* it — real triggers find them fine at runtime, but
// the Apps Script editor's "function to run" dropdown is built by statically scanning the file
// for top-level `function` declarations, and finds none there. These empty stubs exist only to
// be visible to that scanner: top-level function declarations are hoisted and bound to the
// global object before any code runs, then the IIFE's `Object.assign(globalThis, {...})` — which
// executes after hoisting, as the file's first real statement — immediately overwrites each stub
// with the true implementation. Nobody ever calls these bodies.
const ENTRY_POINT_NAMES = ["onHomepage", "onRefresh", "onAction", "debugToken"];

mkdirSync("dist", { recursive: true });
await build({
  entryPoints: ["src/main.ts"],
  outfile: "dist/Code.js",
  bundle: true,
  format: "iife",
  target: "es2019",
  legalComments: "none",
  footer: { js: ENTRY_POINT_NAMES.map((name) => `function ${name}() {}`).join("\n") },
});
cpSync("appsscript.json", "dist/appsscript.json");
console.log("built dist/Code.js and dist/appsscript.json");
