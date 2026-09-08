/* 半双工 / 全双工: 真的跑 avatar.js 里的那段状态机。
 *
 * 这段判反了很难看出来 —— 页面照常, 只是"嘈杂环境里她会跟自己聊起来"。
 * 起因: 音箱里她自己的声音被麦克风听回去, 全双工下就成了自我打断的循环。
 *
 * 跑法: node server/tests/js/avatar_duplex.test.mjs
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "app", "static", "avatar.js");

function makeDom({ stored = null } = {}) {
  const ear = { started: 0, stopped: 0, running: false, onend: null, onresult: null, onerror: null,
    start() { if (this.running) throw new Error("InvalidStateError"); this.running = true; this.started++; },
    stop() { if (!this.running) throw new Error("not running"); this.running = false; this.stopped++;
             if (this.onend) this.onend(); },
    lang: "", continuous: false, interimResults: false };

  const els = {};
  const mk = (id) => {
    const cls = new Set(["av-status"]);
    return (els[id] = {
      id, value: "", textContent: "", hidden: false, disabled: false, style: {},
      className: "", children: [], listeners: {},
      classList: { contains: (c) => cls.has(c), add: (c) => cls.add(c), remove: (c) => cls.delete(c) },
      addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); },
      appendChild(c) { this.children.push(c); return c; },
      removeChild() {}, setAttribute() {}, removeAttribute() {}, focus() {},
      pause() {}, load() {},
    });
  };
  ["#avCall", "#avVideo", "#avBg", "#avHint", "#avLog", "#avStatus", "#avPerson",
   "#avSay", "#avBalance", "#avRate", "#avTimer", "#avCost", "#avDuplex"].forEach(mk);

  const document = {
    documentElement: { lang: "zh" },
    querySelector: (s) => els[s] || null,
    createElement: () => mk("tmp"),
    addEventListener() {},
  };
  const store = { v: stored };
  const window = {
    __T: {}, SpeechRecognition: function () { return ear; },
    localStorage: {
      getItem: (k) => (k === "dhc.avatar.duplex" ? store.v : null),
      setItem: (k, v) => { if (k === "dhc.avatar.duplex") store.v = v; },
    },
    MediaSource: null, ManagedMediaSource: null,
    // boot() 在加载时就会打接口 —— 桩必须是**真的 Promise**, 否则模块自己的
    // .catch 链会炸, 而那与被测的双工逻辑毫无关系。
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
    setInterval: () => 0, clearInterval() {}, setTimeout: () => 0,
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    WebSocket: function () { return { close() {}, send() {} }; },
    location: { protocol: "https:", host: "x", search: "" },
  };
  return { document, window, els, ear, store };
}

/* avatar.js 是 IIFE, 内部函数不外露。把它跑起来之后, 用它自己暴露给 DOM 的
   接口 (选择框的 change 事件) 和内部状态的可观察后果 (识别器被 start/stop)
   来断言 —— 这比读源码字符串强, 因为判反了字符串照样在。 */
function load(dom) {
  const src = readFileSync(SRC, "utf8");
  // 把 IIFE 尾部改成把内部对象抛出来, 只为测试可观察状态。其余一字不改。
  const patched = src.replace(
    /\}\)\(\);\s*$/,
    "  window.__test = { st, setDuplex, showVideo, listen, micGate };\n})();\n"
  );
  assert.notEqual(patched, src, "没能挂上测试钩子 —— IIFE 尾部形状变了");
  const fn = new Function(
    "document", "window", "localStorage", "fetch", "setInterval", "clearInterval",
    "setTimeout", "URL", "WebSocket", "location", "console", patched);
  fn(dom.document, dom.window, dom.window.localStorage, dom.window.fetch,
     dom.window.setInterval, dom.window.clearInterval, dom.window.setTimeout,
     dom.window.URL, dom.window.WebSocket, dom.window.location, console);
  return dom.window.__test;
}

function check(name, fn) {
  try { fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

console.log("数字人 半双工/全双工:");

check("默认半双工", () => {
  const dom = makeDom();
  const api = load(dom);
  assert.equal(api.st.duplex, "half", "默认成了全双工 —— 陌生人第一次用就在嘈杂环境里翻车");
  assert.equal(dom.els["#avDuplex"].value, "half", "选择框没跟着状态");
});

check("记住上次的选择", () => {
  const api = load(makeDom({ stored: "full" }));
  assert.equal(api.st.duplex, "full");
});

check("半双工: 她一开口就闭麦, 说完再开", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};                      // 通话中
  api.listen();
  assert.equal(dom.ear.running, true, "接通后没开始听");

  api.showVideo(true);                 // 她开始说
  assert.equal(dom.ear.running, false, "她说话时麦克风还开着 —— 会把自己听回去");
  api.showVideo(false);                // 她说完
  assert.equal(dom.ear.running, true, "她说完了麦克风没开回来 —— 表现是她突然不理人");
});

check("半双工: 闭麦期间 onend 不能把麦顶开", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.showVideo(true);                 // stop() 内部会触发 onend
  assert.equal(dom.ear.running, false, "onend 里的自动重开把闸顶开了");
});

check("半双工: 她说话期间识别到的话要丢掉", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.st.speaking = true;              // 她正在说
  let replied = false;
  const orig = dom.window.fetch;
  dom.window.fetch = () => { replied = true; return orig(); };
  dom.ear.onresult({ results: [[{ transcript: "从音箱绕回来的她自己" }]] });
  assert.equal(replied, false, "把音箱里绕回来的声音当成了用户说话");
});

check("全双工: 她说话时照样听 (能打断)", () => {
  const dom = makeDom({ stored: "full" });
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.showVideo(true);
  assert.equal(dom.ear.running, true, "全双工被闭麦了 —— 打断功能没了");
});

check("通话中切换立即生效", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.showVideo(true);                 // 半双工, 现在闭着麦
  assert.equal(dom.ear.running, false);

  api.setDuplex("full");               // 走进安静的地方
  assert.equal(dom.ear.running, true, "切到全双工后麦克风没开回来");

  api.setDuplex("half");               // 又走进嘈杂的地方, 而她还在说
  assert.equal(dom.ear.running, false, "切回半双工后麦克风还开着");
  assert.equal(dom.store.v, "half", "没记住这次选择");
});

check("轮次提示不覆盖错误消息", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  const s = dom.els["#avStatus"];
  s.classList.add("av-bad");
  s.textContent = "麦克风被拒绝了";
  api.showVideo(true);
  assert.equal(s.textContent, "麦克风被拒绝了", "错误被每轮都写的提示盖掉了");
});
