/* 真的执行 workspace-chrome.js 的抽屉逻辑, 而不是 grep 它的源码。
 *
 * 起因: 这个 bug 修了两次都没修好, 而两次的"测试"都是在源码里找子串 ——
 * 第二次的断言 `col.contains(e.target)` 连 `!col.contains(e.target)`
 * (正是第一版的错误写法) 都能通过。字符串匹配对判定方向的错误完全无能为力。
 *
 * 跑法: node server/tests/js/sidebar_tap.test.mjs
 * (dhc-server 镜像里没有 node; 用 dsh-local 镜像或本机 node 跑。)
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "app", "static", "pwa", "workspace-chrome.js");

/* ----------------------------------------------------------- 最小 DOM 桩 */
function el(cls = "", extra = {}) {
  const node = {
    className: cls, id: "", style: {}, hidden: false, children: [], parent: null,
    innerHTML: "", textContent: "", listeners: {},
    clicks: 0,
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild(c) { c.parent = node; node.children.push(c); return c; },
    addEventListener(t, fn) { (node.listeners[t] ||= []).push(fn); },
    click() { node.clicks++; (node.listeners.click || []).forEach(f => f({})); },
    focus() {},
    getBoundingClientRect() { return node.rect || { left: 0, right: 0, top: 0, bottom: 0 }; },
    contains(x) {
      for (let p = x; p; p = p.parent) if (p === node) return true;
      return false;
    },
    querySelector(sel) { return matchIn(node, sel); },
    querySelectorAll(sel) { return matchAll(node, sel); },
    ...extra,
  };
  return node;
}
const attrSel = /^\[class\*="([^"]+)"\]$/;
function hit(node, sel) {
  const m = attrSel.exec(sel);
  if (m) return (node.className || "").includes(m[1]);
  if (sel.startsWith("#")) return node.id === sel.slice(1);
  return false;
}
function matchAll(root, sel, out = []) {
  for (const c of root.children) { if (hit(c, sel)) out.push(c); matchAll(c, sel, out); }
  return out;
}
function matchIn(root, sel) { return matchAll(root, sel)[0] || null; }

function makeDom({ narrow = true, collapsed = false } = {}) {
  const body = el("body");
  const frame = body.appendChild(el("pI_x6G_frame"));
  const col = frame.appendChild(el("pI_x6G_sidebarCol"));
  col.rect = { left: 0, right: 260, top: 0, bottom: 800 };
  const root = col.appendChild(el("hHd-Xa_root" + (collapsed ? " hHd-Xa_collapsed" : "")));
  const toggle = root.appendChild(el("hHd-Xa_toggle"));
  const item = root.appendChild(el("hHd-Xa_regionArea"));     // 抽屉里的条目
  const center = frame.appendChild(el("pI_x6G_centerCol"));
  const input = center.appendChild(el("composer"));           // 聊天区里的输入框

  const docListeners = {};
  const document = {
    body, readyState: "complete",
    documentElement: el(),
    // buildChrome 用 innerHTML 造子节点, 假 DOM 里不会真的生成 —— 让新建元素的
    // querySelector 一律回一个桩, 免得那段在这里崩掉。被测的是抽屉逻辑, 不是它。
    createElement: () => {
      const n = el();
      n.querySelector = () => el();
      return n;
    },
    getElementById: id => matchIn(body, "#" + id),
    querySelector: sel => matchIn(body, sel),
    querySelectorAll: sel => matchAll(body, sel),
    addEventListener(t, fn, capture) { (docListeners[t] ||= []).push({ fn, capture }); },
  };
  const window = {
    matchMedia: () => ({ matches: narrow, addEventListener() {} }),
    addEventListener() {},
    location: { search: "", href: "" },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0,
    fetch: () => Promise.reject(new Error("no network in test")),
    MutationObserver: class { observe() {} disconnect() {} },
  };
  return { document, window, docListeners, col, toggle, item, center, input, root };
}

/* --------------------------------------------------------------- 运行 */
function load(dom) {
  const src = readFileSync(SRC, "utf8");
  const fn = new Function(
    "document", "window", "setInterval", "clearInterval", "setTimeout",
    "MutationObserver", "fetch", "location", "sessionStorage", "navigator",
    src);
  fn(dom.document, dom.window, dom.window.setInterval, dom.window.clearInterval,
     dom.window.setTimeout, dom.window.MutationObserver, dom.window.fetch,
     dom.window.location, dom.window.sessionStorage, { userAgent: "test" });
  const entry = (dom.docListeners.click || []).find(l => l.capture === true);
  assert.ok(entry, "没有在捕获阶段注册 document click 处理");
  return entry.fn;
}

function check(name, fn) {
  try { fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

console.log("抽屉外点击 -> 收起:");

check("点聊天区 → 收起抽屉", () => {
  const dom = makeDom();
  load(dom)({ target: dom.center });
  assert.equal(dom.toggle.clicks, 1, "没有收起 —— 正是用户报的那个毛病");
});

check("点输入框 → 收起抽屉 (且不吞掉这次点击)", () => {
  const dom = makeDom();
  let prevented = false;
  load(dom)({ target: dom.input, preventDefault: () => { prevented = true; } });
  assert.equal(dom.toggle.clicks, 1);
  assert.equal(prevented, false, "吞掉了点击 —— 输入框不会聚焦");
});

check("点抽屉内部 → 不收起", () => {
  const dom = makeDom();
  load(dom)({ target: dom.item });
  assert.equal(dom.toggle.clicks, 0, "点抽屉里的条目把抽屉关掉了");
});

check("已经收起时 → 不误触发", () => {
  const dom = makeDom({ collapsed: true });
  load(dom)({ target: dom.center });
  assert.equal(dom.toggle.clicks, 0, "收起状态下又点了一次 toggle, 会把它展开");
});

check("宽屏 → 完全不介入", () => {
  const dom = makeDom({ narrow: false });
  load(dom)({ target: dom.center });
  assert.equal(dom.toggle.clicks, 0, "桌面端点聊天区收起了侧栏");
});
