import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const admin = fs.readFileSync(new URL("../../app/templates/admin.html", import.meta.url), "utf8");
const team = fs.readFileSync(new URL("../../app/templates/team.html", import.meta.url), "utf8");
const app = fs.readFileSync(new URL("../../app/static/app.js", import.meta.url), "utf8");

test("user email is assigned through textContent", () => {
  assert.match(admin, /emailCell\.textContent = u\.email/);
  assert.match(team, /emailCell\.textContent = u\.email/);
  assert.doesNotMatch(admin, /innerHTML[\s\S]{0,180}u\.email/);
  assert.doesNotMatch(team, /innerHTML[\s\S]{0,180}u\.email/);
});

test("API model fields never flow through HTML parsing", () => {
  assert.doesNotMatch(app, /innerHTML/);
  assert.match(app, /name\.textContent = m\.name/);
  assert.match(app, /name\.title = m\.id/);
});
