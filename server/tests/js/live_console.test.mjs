/* 直播控制台: 15 秒一次的刷新**不能把没保存的改动顶回去**。
 *
 * 真实事故 (创始人 2026-09-11): 控制台上标题写着「双11天猫大促」, 话术却是一整套
 * 伴聊词。操作序列是 —— 在标题框输入新主题 → 点「生成话术」(焦点落到按钮上) →
 * 15 秒内的刷新把标题**无声**顶回服务端那份旧的 → 点保存 = 旧标题 + 新话术。
 * 同一个洞还有更糟的一面: 生成出来的话术若 15 秒内没保存、光标又不在文本框里,
 * **生成的结果本身也会被顶掉**, 而页面上什么都不说。
 *
 * 原来的判据是 `document.activeElement !== 这个框`, 焦点一离开就失效。形象下拉早就
 * 用 presetInit 防住了同一件事, 这两个框只是没跟上。
 *
 * ⚠️ 反方向同样要守: 没动过的框必须跟随服务端(否则两个人同时改就看不到对方的),
 * 存过之后也要恢复跟随(否则控制台会永远停在本地那份上)。
 *
 * 跑法: node server/tests/js/live_console.test.mjs
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "app", "static", "live_console.js");

function makeDom() {
  const el = (id) => ({
    id, value: "", textContent: "", hidden: false, disabled: false,
    className: "", dataset: {}, listeners: {}, children: [],
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); },
    removeEventListener() {},
    setAttribute() {}, removeAttribute() {},
    appendChild(c) { this.children.push(c); return c; },
    focus() { dom.document.activeElement = this; },
    fire(t, ev = {}) { (this.listeners[t] || []).forEach((f) => f(ev)); },
  });
  const ids = {};
  for (const k of ["lvName", "lvPreset", "lvScript", "lvCounts", "lvSay", "lvRecast",
                   "lvComment", "lvMode", "lvLog", "lvSave", "lvStart", "lvStop",
                   "lvGen", "lvSend"]) ids[k] = el(k);
  // 下拉要有 options, selectPreset 会遍历
  ids.lvPreset.options = [{ value: "default", dataset: { person: "source-v3-head", voice: "xiaoya" } }];
  ids.lvPreset.selectedIndex = 0;

  // 服务端那份。room 是 GET /api/live/room 回的东西。
  const server = { title: "双11天猫大促", lines: ["旧话术第一句"], person: "source-v3-head",
                   voice: "xiaoya", live: true, said: 0, recent: [] };
  const gen = { lines: ["伴聊第一句", "伴聊第二句"] };
  const calls = [];
  const timers = [];

  const document = {
    getElementById: (i) => ids[i] || null,
    createElement: () => el("div"),
    addEventListener() {},
    activeElement: null,          // 焦点不在任何框里 —— 这正是 bug 的触发条件
  };
  const window = {
    fetch: (url, opt) => {
      calls.push({ url, method: (opt && opt.method) || "GET", body: opt && opt.body });
      let data = {};
      const path = String(url).split("?")[0];               // 多间之后每个调用都带 ?room=
      if (path === "/api/live/room" && (!opt || !opt.method)) data = server;
      else if (path === "/api/live/generate") data = gen;
      else if (path === "/api/live/room") {                 // PUT = 保存
        const b = JSON.parse(opt.body);
        server.title = b.title; server.lines = b.lines;
        data = { ok: true };
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
    },
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    LivePlayer: null,
  };
  const dom = { document, window, ids, server, gen, calls, timers,
                tick15: () => timers.filter((t) => t.ms === 15000).forEach((t) => t.fn()) };
  return dom;
}

function load(dom) {
  const src = readFileSync(SRC, "utf8");
  new Function("document", "window", "fetch", "setInterval", src)(
    dom.document, dom.window, dom.window.fetch, dom.window.setInterval);
}
const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };

async function check(name, fn) {
  try { await fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

console.log("控制台: 刷新不能顶掉没保存的改动");

await check("在标题框改了字 -> 15 秒刷新**不能**顶回去 (就是那次标题话术对不上)", async () => {
  const dom = makeDom();
  load(dom); await settle();
  assert.equal(dom.ids.lvName.value, "双11天猫大促", "首次没把服务端的标题落进来");

  dom.ids.lvName.value = "伴聊直播间";
  dom.ids.lvName.fire("input");
  dom.document.activeElement = null;        // 点了「生成话术」, 焦点离开标题框
  dom.tick15(); await settle();

  assert.equal(dom.ids.lvName.value, "伴聊直播间",
    "标题被无声顶回服务端那份 —— 接着按保存就是「旧标题 + 新话术」");
});

await check("刚生成的话术 -> 15 秒刷新**不能**顶掉 (光标不在文本框里也一样)", async () => {
  const dom = makeDom();
  load(dom); await settle();

  dom.ids.lvGen.fire("click"); await settle();
  assert.equal(dom.ids.lvScript.value, "伴聊第一句\n伴聊第二句", "生成的话术没填进框");

  dom.document.activeElement = null;
  dom.tick15(); await settle();
  assert.equal(dom.ids.lvScript.value, "伴聊第一句\n伴聊第二句",
    "生成的话术被顶掉了, 而页面一声不吭 —— 人以为存的是新版, 其实存的是旧版");
});

await check("⛔ 反向: 没动过的框必须跟随服务端 (否则看不到别人的改动)", async () => {
  const dom = makeDom();
  load(dom); await settle();
  dom.server.title = "别人改的标题";
  dom.server.lines = ["别人改的话术"];
  dom.tick15(); await settle();
  assert.equal(dom.ids.lvName.value, "别人改的标题", "没动过的标题却不跟随服务端了");
  assert.equal(dom.ids.lvScript.value, "别人改的话术", "没动过的话术却不跟随服务端了");
});

await check("⛔ 反向: 存过之后要恢复跟随 (否则控制台永远停在本地那份上)", async () => {
  const dom = makeDom();
  load(dom); await settle();

  dom.ids.lvName.value = "我的新标题";
  dom.ids.lvName.fire("input");
  dom.ids.lvSave.fire("click"); await settle();
  assert.equal(dom.server.title, "我的新标题", "保存没把标题送上去");

  dom.server.title = "保存之后别人又改的";
  dom.tick15(); await settle();
  assert.equal(dom.ids.lvName.value, "保存之后别人又改的",
    "存过了还赖着本地那份 —— 之后所有人的改动这台机器都看不见");
});

await check("保存送上去的标题和话术必须是框里当时的那一版", async () => {
  const dom = makeDom();
  load(dom); await settle();
  dom.ids.lvName.value = "伴聊直播间";
  dom.ids.lvName.fire("input");
  dom.ids.lvGen.fire("click"); await settle();
  dom.document.activeElement = null;
  dom.tick15(); await settle();              // 中间插一次刷新, 模拟真实时序
  dom.ids.lvSave.fire("click"); await settle();

  assert.equal(dom.server.title, "伴聊直播间", `存进去的标题是 ${dom.server.title}`);
  assert.deepEqual(dom.server.lines, ["伴聊第一句", "伴聊第二句"],
    `存进去的话术是 ${JSON.stringify(dom.server.lines)}`);
});
