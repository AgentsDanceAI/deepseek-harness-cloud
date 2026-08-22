#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..", "..");
const release = JSON.parse(fs.readFileSync(path.join(root, "release", "release.json"), "utf8"));
const upstream = JSON.parse(fs.readFileSync(path.join(root, "desktop", "upstream.json"), "utf8"));

assert.match(release.version, /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/);
assert.equal(
  upstream.runtimePackageVersion,
  release.desktopRuntime,
  "desktop runtime must match release.desktopRuntime",
);

console.log(`desktop version contract ok: ${release.version} / ${release.desktopRuntime}`);
