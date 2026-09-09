/* 字幕: 只显示正在说的那一句。
 *
 * 第一版把最近十几句堆在右边滚动 —— 结果三分之一画面被占满, 而且**盖住了声音
 * 按钮** (按钮 hidden 到起播才出现, 被盖住更难发现)。创始人 2026-09-09 撞到。
 *
 * 跑法: node server/tests/js/live_captions.test.mjs
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "app", "static", "live_captions.js");
const CSS = join(here, "..", "..", "app", "static", "live.css");

function makeDom() {
  const mk = (id) => {
    const el = {
      id, textContent: "", hidden: false, className: "", dataset: {}, style: {},
      children: [], listeners: {}, parentNode: null, scrollTop: 0, scrollHeight: 0,
      addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); },
      setAttribute() {},
      appendChild(c) { c.parentNode = this; this.children.push(c); this.textContent += c.textContent; return c; },
      removeChild(c) { this.children = this.children.filter((x) => x !== c); },
      fire(t) { (this.listeners[t] || []).forEach((f) => f({})); },
    };
    // textContent = "" 要真的清空子节点, 否则测不出"换掉上一句"
    Object.defineProperty(el, "textContent", {
      get() { return this._tc || ""; },
      set(v) { this._tc = v; if (v === "") this.children = []; },
    });
    return el;
  };
  const ids = { lvVideo: mk("lvVideo"), lvCaps: mk("lvCaps"), lvCapsToggle: mk("lvCapsToggle") };
  ids.lvVideo.parentNode = mk("stage");
  ids.lvCapsToggle.dataset = { hide: "关字幕", show: "开字幕" };
  let payload = { live: true, lines: [] };
  const document = {
    getElementById: (i) => ids[i] || null,
    addEventListener() {},
    // ⚠️ 少了它, add() 里的 createElement 会抛, 而整条链路上的 .catch 会把异常
    // 吞掉 —— 表现是"测试红了但看不出为什么"。
    createElement: () => mk("div"),
  };
  const window = {
    localStorage: { getItem: () => null, setItem() {} },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
    setInterval: () => 0,
  };
  return { document, window, ids, set: (p) => { payload = p; } };
}

function load(dom) {
  const src = readFileSync(SRC, "utf8");
  const fn = new Function("document", "window", "fetch", "setInterval", "localStorage", src);
  fn(dom.document, dom.window, dom.window.fetch, dom.window.setInterval, dom.window.localStorage);
  return dom.window.LiveCaptions;
}
const tick = () => new Promise((r) => setImmediate(r));

async function check(name, fn) {
  try { await fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

console.log("直播字幕:");

await check("只留正在说的那一句, 不堆叠", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [
    { t: 1, kind: "script", text: "第一句" },
    { t: 2, kind: "script", text: "第二句" },
    { t: 3, kind: "script", text: "第三句" }] });
  await api.pull();
  const box = dom.ids.lvCaps;
  assert.equal(box.children.length, 1, `堆了 ${box.children.length} 句 —— 会占满右侧并盖住声音按钮`);
  assert.equal(box.children[0].textContent, "第三句", "留的不是最新那句");
});

await check("下一句到了要换掉上一句", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [{ t: 1, kind: "script", text: "旧的" }] });
  await api.pull();
  dom.set({ live: true, lines: [{ t: 2, kind: "script", text: "新的" }] });
  await api.pull();
  assert.equal(dom.ids.lvCaps.children.length, 1);
  assert.equal(dom.ids.lvCaps.children[0].textContent, "新的");
});

await check("回评论那句要能被认出来", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [{ t: 9, kind: "interject", text: "回你这条" }] });
  await api.pull();
  assert.match(dom.ids.lvCaps.children[0].className, /lv-cap--in/);
});

await check("停播清空 —— 别把上一场的字留在屏幕上", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [{ t: 1, kind: "script", text: "还在播" }] });
  await api.pull();
  dom.set({ live: false, lines: [] });
  await api.pull();
  assert.equal(dom.ids.lvCaps.children.length, 0);
});

await check("字幕层不能压住声音按钮", async () => {
  const css = readFileSync(CSS, "utf8");
  const grab = (sel) => {
    const i = css.indexOf(sel);
    assert.ok(i >= 0, `CSS 里没有 ${sel}`);
    return css.slice(i, css.indexOf("}", i));
  };
  const caps = grab(".lv-caps {"), unmute = grab(".lv-unmute {");
  const z = (b) => { const m = /z-index:\s*(\d+)/.exec(b); return m ? +m[1] : 0; };
  assert.ok(z(unmute) > z(caps),
    `声音按钮 z-index ${z(unmute)} 不高于字幕层 ${z(caps)} —— 会被盖住, 而它起播才出现, 更难发现`);
  assert.ok(!/top:\s*0/.test(caps), "字幕又铺满整条右侧了, 会盖住右下角的按钮");
});
