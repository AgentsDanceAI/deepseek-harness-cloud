/* 在场心跳: 真的执行 workspace-chrome.js 里的那段, 不是 grep 源码。
 *
 * 这段逻辑决定"一台工作台什么时候被回收", 而机时是按容器存在时间计费的 ——
 * 判反了两个方向都要出事:
 *   多报: 忘了关的标签页整夜续租, 烧的是用户的额度;
 *   漏报: 人正在用却被判成不在, 十分钟就被踢下线 (还顺带一次冷启动)。
 *
 * 跑法: node server/tests/js/presence.test.mjs
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "app", "static", "pwa", "workspace-chrome.js");

function makeDom({ hidden = false } = {}) {
  const listeners = {};
  const intervals = [];
  const posts = [];
  const node = () => ({
    className: "", id: "", style: {}, hidden: false, children: [], parent: null,
    innerHTML: "", textContent: "", listeners: {},
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    addEventListener() {}, click() {}, focus() {},
    getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0 }; },
    contains() { return false; },
    querySelector() { return node(); }, querySelectorAll() { return []; },
  });
  const document = {
    body: node(), documentElement: node(), readyState: "complete",
    hidden,
    createElement: () => node(),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(t, fn) { (listeners[t] ||= []).push(fn); },
  };
  const window = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    addEventListener() {},
    location: { search: "", href: "" },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    clearInterval() {}, setTimeout: () => 0,
    fetch: (url, opt) => { posts.push({ url, opt }); return Promise.resolve({ json: () => ({}) }); },
    MutationObserver: class { observe() {} disconnect() {} },
  };
  return { document, window, listeners, intervals, posts };
}

function load(dom) {
  const src = readFileSync(SRC, "utf8");
  const fn = new Function(
    "document", "window", "setInterval", "clearInterval", "setTimeout",
    "MutationObserver", "fetch", "location", "sessionStorage", "navigator", src);
  fn(dom.document, dom.window, dom.window.setInterval, dom.window.clearInterval,
     dom.window.setTimeout, dom.window.MutationObserver, dom.window.fetch,
     dom.window.location, dom.window.sessionStorage, { userAgent: "test" });
  // 心跳那一条 —— 按周期挑出来, 免得和 dsh composer 的 500ms 轮询混在一起
  const beat = dom.intervals.find(i => i.ms >= 30000);
  assert.ok(beat, "没有注册在场心跳");
  return {
    tick: () => beat.fn(),
    fire: (type, ev = {}) => (dom.listeners[type] || []).forEach(f => f(ev)),
    beats: () => dom.posts.filter(p => String(p.url).includes("/api/work/active")),
  };
}

function check(name, fn) {
  try { fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

console.log("在场心跳:");

check("没人动过 → 不上报", () => {
  const h = load(makeDom());
  h.tick(); h.tick(); h.tick();
  assert.equal(h.beats().length, 0,
    "光靠定时器就上报了 —— 等于把'标签页开着'当成'有人在', 空页面会整夜烧机时");
});

check("按了键 → 上报一次", () => {
  const h = load(makeDom());
  h.fire("keydown");
  h.tick();
  assert.equal(h.beats().length, 1);
  assert.equal(h.beats()[0].opt.method, "POST");
  assert.equal(h.beats()[0].opt.credentials, "include", "不带凭据 —— 服务端认不出是谁");
});

check("动一次只续一次, 不会每个周期都发", () => {
  const h = load(makeDom());
  h.fire("pointerdown");
  h.tick(); h.tick(); h.tick();
  assert.equal(h.beats().length, 1, "一次动作被算成了三次在场");
});

check("再动 → 再续", () => {
  const h = load(makeDom());
  h.fire("keydown"); h.tick();
  h.fire("wheel");   h.tick();
  assert.equal(h.beats().length, 2);
});

check("页面在后台 → 不上报", () => {
  const h = load(makeDom({ hidden: true }));
  h.fire("keydown");
  h.tick();
  assert.equal(h.beats().length, 0, "切走的标签页还在续租");
});

check("切回前台本身算一次在场", () => {
  const dom = makeDom({ hidden: true });
  const h = load(dom);
  dom.document.hidden = false;
  h.fire("visibilitychange");
  h.tick();
  assert.equal(h.beats().length, 1, "人回来了却要再动一下才算在场");
});

check("触屏也算 (手机上没有 mousemove)", () => {
  const h = load(makeDom());
  h.fire("touchstart");
  h.tick();
  assert.equal(h.beats().length, 1);
});
