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

  const rafq = [];
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
    setInterval: () => 0, clearInterval() {},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
    // 手动驱动的动画帧队列 —— 露出视频层要等它, 测试里得能一帧一帧推
    requestAnimationFrame: (fn) => { rafq.push(fn); return rafq.length; },
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    WebSocket: function () { return { close() {}, send() {} }; },
    location: { protocol: "https:", host: "x", search: "" },
  };
  // 推进 n 个动画帧
  const pump = (n = 1) => {
    for (let i = 0; i < n; i++) { const q = rafq.splice(0); q.forEach((f) => f()); }
  };
  return { document, window, els, ear, store, rafq, pump };
}

/* avatar.js 是 IIFE, 内部函数不外露。把它跑起来之后, 用它自己暴露给 DOM 的
   接口 (选择框的 change 事件) 和内部状态的可观察后果 (识别器被 start/stop)
   来断言 —— 这比读源码字符串强, 因为判反了字符串照样在。 */
function load(dom) {
  const src = readFileSync(SRC, "utf8");
  // 把 IIFE 尾部改成把内部对象抛出来, 只为测试可观察状态。其余一字不改。
  const patched = src.replace(
    /\}\)\(\);\s*$/,
    "  window.__test = { st, setDuplex, showVideo, listen, micGate, fill, loadBg, layout,\n"
    + "                     herSpoke, isEcho, MIC_REOPEN_MS };\n})();\n"
  );
  assert.notEqual(patched, src, "没能挂上测试钩子 —— IIFE 尾部形状变了");
  const fn = new Function(
    "document", "window", "localStorage", "fetch", "setInterval", "clearInterval",
    "setTimeout", "clearTimeout", "URL", "WebSocket", "location", "console",
    "requestAnimationFrame", patched);
  fn(dom.document, dom.window, dom.window.localStorage, dom.window.fetch,
     dom.window.setInterval, dom.window.clearInterval, dom.window.setTimeout,
     dom.window.clearTimeout,
     dom.window.URL, dom.window.WebSocket, dom.window.location, console,
     dom.window.requestAnimationFrame);
  return dom.window.__test;
}

function check(name, fn) {
  try { fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

/* 开麦保护期是真的等一会儿, 所以这几条必须异步 —— 用假定时器的话就等于没测到
   "它确实等了", 而那正是这条改动的全部内容。 */
async function checkAsync(name, fn) {
  try { await fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 只盯 /api/avatar/say 那一条 —— 「她有没有把这句当成用户说话」就看它。
   ⚠️ 别整个 fetch 替掉: 模块启动时自己会打一次接口, 替成没有 .json() 的桩会让
   boot 当场抛错, 而且把那一次算进"被调用过", 于是这类用例永远是错的绿/错的红。 */
function sayProbe(dom) {
  const probe = { hit: false, said: "" };
  dom.window.fetch = (url, opt) => {
    if (String(url).includes("/api/avatar/say")) {
      probe.hit = true;
      try { probe.said = JSON.parse(opt.body).text; } catch { /* 不关心 */ }
      return Promise.resolve({ ok: false });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  };
  return probe;
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

await checkAsync("半双工: 她一开口就闭麦, 说完等一下再开", async () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};                      // 通话中
  api.listen();
  assert.equal(dom.ear.running, true, "接通后没开始听");

  api.showVideo(true);                 // 她开始说
  assert.equal(dom.ear.running, false, "她说话时麦克风还开着 —— 会把自己听回去");

  api.showVideo(false);                // 她说完
  // **不能立刻开**: 画面停住那一刻声音还在往外走 (设备输出缓冲 + 蓝牙 100~300ms
  // + 房间混响), 立刻开麦收到的就是她最后半个字。
  assert.equal(dom.ear.running, false, "她话音刚落就开麦了 —— 会把音箱的尾巴收进来");
  await sleep(api.MIC_REOPEN_MS + 120);
  assert.equal(dom.ear.running, true, "保护期过了麦克风还没开回来 —— 表现是她突然不理人");
});

await checkAsync("半双工: 保护期里她又开口, 就不该开麦", async () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.showVideo(true);
  api.showVideo(false);                // 她"说完了"(其实只是卡了一下)
  api.showVideo(true);                 // 保护期内又接着说
  await sleep(api.MIC_REOPEN_MS + 120);
  assert.equal(dom.ear.running, false,
    "定时器到点还是把麦开了 —— 她正说着话, 这一开就是把自己听回去");
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

/* 下面这组是**内容闸**: 与她说没说、麦开没开都无关。
   时序闸有两个躲不掉的漏点(声音比画面慢、中途卡顿被当成说完), 而全双工压根没有
   时序闸 —— 所以按内容认回声是唯一盖得全的一道。 */
check("回声闸: 听回来的就是她刚说过的那句, 丢掉", () => {
  const dom = makeDom();
  const probe = sayProbe(dom);
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.herSpoke("你先看懂它到底能解决你什么问题，再决定要不要");
  api.st.speaking = false;             // 时序闸此刻是开的 —— 只有内容闸能拦
  dom.ear.onresult({ results: [[{ transcript: "你先看懂它到底能解决你什么问题" }]] });
  assert.equal(probe.hit, false, "她自己的话绕回来被当成了用户说话");
});

check("回声闸: 识别器把两句连着吐出来也认得出", () => {
  const dom = makeDom();
  const probe = sayProbe(dom);
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.herSpoke("我在呢");
  api.st.speaking = false;
  dom.ear.onresult({ results: [[{ transcript: "我在呢，你说" }]] });
  assert.equal(probe.hit, false, "只比对完全相等 —— 识别器多听/少听一点就漏了");
});

check("回声闸: 真人说的话不能被误杀", () => {
  const dom = makeDom();
  const probe = sayProbe(dom);
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.herSpoke("你先看懂它到底能解决你什么问题");
  api.st.speaking = false;
  dom.ear.onresult({ results: [[{ transcript: "帮我看看这个多少钱" }]] });
  assert.equal(probe.hit, true, "把真人说的话当回声丢了 —— 用户会以为她不理人");
});

check("回声闸: 短句一律放行", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.herSpoke("好的，那我们继续");
  assert.equal(api.isEcho("好的"), false,
    "「好的」这种谁都会说的短句被当成回声 —— 用户附和一句就被吞掉");
});

check("回声闸: 太久以前说的不算 (声音早散了)", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.herSpoke("这件事比抢到手要紧得多");
  api.st.herSaid[0].at -= 60000;       // 一分钟前说的
  assert.equal(api.isEcho("这件事比抢到手要紧得多"), false,
    "一分钟前的话还在拦 —— 用户复述她说过的内容会被永久吞掉");
});

check("回声闸: 记的句数有上限, 不会一直涨", () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  for (let i = 0; i < 30; i++) api.herSpoke("这是第" + i + "句话说得挺长的");
  assert.ok(api.st.herSaid.length <= 6,
    "她说过的话无上限地攒着 —— 长通话会把内存和比对成本一起拖大");
  assert.equal(api.isEcho("这是第29句话说得挺长的"), true, "最近说的那句反而没留住");
});

check("全双工: 她说话时照样听 (能打断)", () => {
  const dom = makeDom({ stored: "full" });
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.showVideo(true);
  assert.equal(dom.ear.running, true, "全双工被闭麦了 —— 打断功能没了");
});

await checkAsync("通话中切换立即生效", async () => {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.listen();
  api.showVideo(true);                 // 半双工, 现在闭着麦
  assert.equal(dom.ear.running, false);

  api.setDuplex("full");               // 走进安静的地方
  await sleep(api.MIC_REOPEN_MS + 120);   // 开麦有保护期, 见 micGate
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

/* --------------------------------------------------- 换人之后别被冲回默认 */
console.log("\n形象选择:");

check("重建选项时保住已经选好的人", () => {
  const dom = makeDom();
  const api = load(dom);
  const sel = dom.els["#avPerson"];
  // fill 用的是真 DOM 的 options/value 语义, 桩里补上最小实现
  sel.options = [];
  sel.appendChild = function (o) { this.options.push(o); if (this.options.length === 1) this.value = o.value; return o; };
  Object.defineProperty(sel, "innerHTML", { set() { this.options = []; this.value = ""; }, get() { return ""; } });

  const names = { lin: "林 · 安静", yue: "悦 · 干练" };
  api.fill(sel, ["lin", "yue"], "source-v3-head", names);
  sel.value = "lin";                       // 用户选了林

  api.fill(sel, ["lin", "yue"], "source-v3-head", names);   // 挂断后 boot() 又跑一次
  assert.equal(sel.value, "lin",
    "选好的人被冲回默认了 —— 下一通电话又是初雪, 也就是用户说的\"脸还是之前的\"");
});

check("选过的人被下架了就落回默认, 不留一个选不中的值", () => {
  const dom = makeDom();
  const api = load(dom);
  const sel = dom.els["#avPerson"];
  sel.options = [];
  sel.appendChild = function (o) { this.options.push(o); if (this.options.length === 1) this.value = o.value; return o; };
  Object.defineProperty(sel, "innerHTML", { set() { this.options = []; this.value = ""; }, get() { return ""; } });

  api.fill(sel, ["lin", "yue"], "d", {});
  sel.value = "lin";
  api.fill(sel, ["yue"], "d", {});          // 林没了
  assert.equal(sel.value, "", "留了一个清单里没有的值, value 与显示的项对不上");
});

/* ------------------------------------------- 露出视频层不能抢在排版之前 */
console.log("\n视频层露出时机:");

function talkingDom() {
  const dom = makeDom();
  const api = load(dom);
  api.st.ws = {};
  api.st.cfg = { crop: { x: 0.1, y: 0.1, w: 0.2, h: 0.4 }, person_crops: {} };
  const v = dom.els["#avVideo"];
  v.videoWidth = 0; v.readyState = 0; v.style.opacity = "0"; v.style.width = "";
  return { dom, api, v };
}

check("还没解出第一帧 → 不露出 (那一帧会以原始尺寸糊满画面)", () => {
  const { dom, api, v } = talkingDom();
  api.showVideo(true);
  dom.pump(3);
  assert.equal(v.style.opacity, "0",
    "视频元素还没套上盒子就显出来了 —— 就是录屏第 82 帧那个放大两倍的方块");
});

check("有了第一帧 → 先重算盒子, 再等一个动画帧才露出", () => {
  const { dom, api, v } = talkingDom();
  api.showVideo(true);
  dom.pump(1);
  v.videoWidth = 512; v.readyState = 2;      // 第一帧到了
  dom.pump(1);                                // 这一帧发现就绪并重排
  assert.equal(v.style.width, "20%", "露出前没有按当前形象重算盒子");
  assert.equal(v.style.opacity, "0", "没等排版落地就露出来了");
  dom.pump(1);
  assert.equal(v.style.opacity, "1", "排完版还是不出画");
});

check("等的过程中她说完了 → 不要再露出来", () => {
  const { dom, api, v } = talkingDom();
  api.showVideo(true);
  dom.pump(1);
  v.videoWidth = 512; v.readyState = 2;
  api.showVideo(false);                       // 话说完了
  dom.pump(3);
  assert.equal(v.style.opacity, "0", "她已经不说了, 画面还是亮了出来");
});

check("一直等不到第一帧也要兜底显出来, 不能永远黑着", () => {
  const { dom, api, v } = talkingDom();
  api.showVideo(true);
  dom.pump(40);                               // 超过 30 帧的上限
  assert.equal(v.style.opacity, "1", "视频永远不出画 —— 比闪一下糟得多");
});
