#!/usr/bin/env node
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.join(here, "..", "capacitor.config.ts"), "utf8");

assert.match(source, /appId:\s*["']ai\.agentsdance\.dshcloud\.app["']/);
assert.match(source, /["']https:\/\/dshcloud\.online["']/);
assert.match(source, /allowNavigation:\s*\[[\s\S]*["']dshcloud\.online["'][\s\S]*["']\*\.dshcloud\.online["'][\s\S]*\]/);
assert.match(source, /allowMixedContent:\s*false/);
assert.doesNotMatch(source, /allowMixedContent:\s*true/);
assert.doesNotMatch(source, /cleartext:\s*true/);

console.log("mobile WebView contract ok");
