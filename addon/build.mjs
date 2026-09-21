// Bundles src/ into one plain script for Apps Script, which has no module system: every function
// it should call by name (the manifest's triggers) is attached to the global object in src/main.ts.
import { build } from "esbuild";
import { cpSync, mkdirSync } from "node:fs";

mkdirSync("dist", { recursive: true });
await build({
  entryPoints: ["src/main.ts"],
  outfile: "dist/Code.js",
  bundle: true,
  format: "iife",
  target: "es2019",
  legalComments: "none",
});
cpSync("appsscript.json", "dist/appsscript.json");
console.log("built dist/Code.js and dist/appsscript.json");
