#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const target = path.join(
  here,
  "..",
  "node_modules",
  "@capacitor",
  "cli",
  "dist",
  "util",
  "template.js",
);
const source = fs.readFileSync(target, "utf8");
const legacy = "tar_1.default.extract({ file: src, cwd: dir })";
const compatible = "(tar_1.default ?? tar_1).extract({ file: src, cwd: dir })";

if (source.includes(compatible)) {
  console.log("Capacitor tar compatibility patch already applied");
} else if (source.includes(legacy)) {
  fs.writeFileSync(target, source.replace(legacy, compatible));
  console.log("Applied Capacitor 6 compatibility patch for maintained node-tar");
} else {
  throw new Error("Unsupported @capacitor/cli template loader; review the tar compatibility patch");
}
