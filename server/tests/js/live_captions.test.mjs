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
  // 默认: 没有 seekable = 落后 0 秒 (老用例就是在这个前提下写的)
  ids.lvVideo.currentTime = 0;
  ids.lvVideo.seekable = { length: 0, end: () => 0 };
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

/* 三行字幕里"正在说"的那一行 —— 断言要盯它, 不能再盯 children[0]
   (children[0] 现在是**上一句**)。 */
function nowLine(dom) {
  const hit = dom.ids.lvCaps.children.filter((c) => /lv-cap--now/.test(c.className));
  return hit.length ? hit[0].textContent : null;
}

async function check(name, fn) {
  try { await fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

console.log("直播字幕:");

await check("只留一句, 而且是**正在播**的那一句 (不是最新那一句)", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [
    { t: 1, kind: "script", text: "第一句" },
    { t: 2, kind: "script", text: "第二句" },
    { t: 3, kind: "script", text: "第三句" }] });
  await api.pull();
  const box = dom.ids.lvCaps;
  assert.ok(box.children.length <= 3,
    `堆了 ${box.children.length} 行 —— 上限是三行(上一句/正在说/下一句), 再多会盖住声音按钮`);
  // 上游是在句子"发出去"时记账的, 而队列里始终压着两句 —— 最新那条还没播。
  // 取最新的表现是字幕比声音早两句, 观众看到的字和听到的话对不上。
  assert.equal(nowLine(dom), "第二句",
    "取了最新那一句 —— 那条还在队列里没播, 字幕会比声音早两句");
});

await check("只有一句时也要显示 —— 刚开播就是这种情况", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [{ t: 1, kind: "script", text: "开播第一句" }] });
  await api.pull();
  assert.equal(dom.ids.lvCaps.children.length, 1, "只有一句时字幕是空的");
  assert.equal(nowLine(dom), "开播第一句");
});

await check("下一句到了要换掉上一句", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [{ t: 1, kind: "script", text: "旧的" }] });
  await api.pull();
  dom.set({ live: true, lines: [{ t: 2, kind: "script", text: "新的" }] });
  await api.pull();
  assert.equal(nowLine(dom), "新的");
});

await check("回评论那句要能被认出来", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: [{ t: 9, kind: "interject", text: "回你这条" }] });
  await api.pull();
  const inl = dom.ids.lvCaps.children.filter((c) => /lv-cap--in/.test(c.className));
  assert.ok(inl.length, "回评论那句没被标出来");
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

/* ── 时间轴对齐 ────────────────────────────────────────────────────────────
 *
 * 2026-09-10 创始人第二次报"字幕对不上"。上一版按"倒数第二条"取, 那只在观众正好
 * 贴着直播边缘时才成立 —— 而播放器是**故意**退后十几秒的 (liveSyncDurationCount=12,
 * 卡顿恢复退到 BEHIND_LIVE=10), 一句约 8-9 秒, 于是字幕比声音早了一句半。
 */
function setLag(dom, seconds) {
  const v = dom.ids.lvVideo;
  v.currentTime = 1000 - seconds;
  v.seekable = { length: 1, end: () => 1000 };
}
// 5 句, 每句 8.6 秒 —— 线上实测的句长
const FIVE = [0, 8.6, 17.2, 25.8, 34.4].map((t, i) => ({ t, kind: "script", text: "第" + (i + 1) + "句" }));

console.log("字幕时间轴对齐:");

await check("贴着边缘 (lag=0) 时取倒数第二条 —— 与上一版一致, 别改坏了", async () => {
  const dom = makeDom();
  const api = load(dom);
  assert.equal(api.pick(FIVE, 34.4, 0, 0), 3, "lag=0 的口径变了");
});

await check("播放器退后 12 秒时必须再往回退 —— 这就是「字幕对不上」", async () => {
  const dom = makeDom();
  const api = load(dom);
  const idx = api.pick(FIVE, 34.4, 0, 12);
  assert.ok(idx < 3, `lag=12 仍然取到第 ${idx} 条 —— 字幕比声音早了一句半`);
  assert.equal(idx, 1, `按时间轴该落在第 1 条 (t=8.6), 实际第 ${idx} 条`);
});

await check("端到端: 落后 12 秒时屏幕上不是倒数第二句", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  setLag(dom, 12);
  dom.set({ live: true, lines: FIVE });
  await api.pull();
  const shown = nowLine(dom);
  assert.notEqual(shown, "第4句", "还是取了倒数第二条 —— lag 没被算进去");
  assert.equal(shown, "第2句", `落后 12 秒该显示第 2 句, 实际显示 ${shown}`);
});

await check("轮询本身的延迟要补回来, 不能算进 lag 里", async () => {
  const dom = makeDom();
  const api = load(dom);
  // 数据是 5 秒前收到的 —— 服务端此刻已经往前走了 5 秒, 字幕也该跟着往前
  const a = api.pick(FIVE, 34.4, 0, 12);
  const b = api.pick(FIVE, 34.4, 5, 12);
  assert.ok(b >= a, `收到数据后过了 5 秒, 字幕反而往回退了 (${a} -> ${b})`);
});

await check("观众往回拖进度条, 字幕要跟着退回去", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  setLag(dom, 0);
  dom.set({ live: true, lines: FIVE });
  await api.pull();
  assert.equal(nowLine(dom), "第4句");
  setLag(dom, 26);                        // 往回拖了 26 秒
  await api.pull();
  assert.ok(dom.ids.lvCaps.children.length <= 3, "回退后堆了超过三行");
  assert.equal(nowLine(dom), "第1句",
    "退回去之后字幕没跟着退 —— 旧实现的 seen 去重会把它挡住");
});

await check("lag 离谱 (取不到 seekable 之类) 时不能把字幕甩飞", async () => {
  const dom = makeDom();
  const api = load(dom);
  assert.equal(api.pick(FIVE, 34.4, 0, 99999), 0, "lag 异常时应停在最早那句, 而不是越界");
  assert.equal(api.pick([], 0, 0, 0), -1, "空表应返回 -1");
  assert.equal(api.pick([FIVE[0]], 0, 0, 12), 0, "只有一句时无论 lag 都显示它");
});

await check("三行: 上一句 / 正在说 / 下一句, 中间那句加重", async () => {
  /* 只留一句时, 中途进来的观众没有上下文; 堆十几句又会占满右侧盖住声音按钮。
     三行是折中 —— .lv-caps 底部留的 56px 正好容得下。 */
  const dom = makeDom();
  const api = load(dom);
  await tick();
  setLag(dom, 0);
  dom.set({ live: true, lines: FIVE });
  await api.pull();
  const kids = dom.ids.lvCaps.children;
  assert.equal(kids.length, 3, `显示了 ${kids.length} 行, 应该是三行`);
  assert.deepEqual(kids.map((c) => c.textContent), ["第3句", "第4句", "第5句"],
    "三行的内容或顺序不对 —— 应该是上一句/正在说/下一句");
  assert.match(kids[1].className, /lv-cap--now/, "中间那行没加重, 观众分不出该看哪行");
  assert.match(kids[0].className, /lv-cap--dim/, "上一句没压暗");
  assert.match(kids[2].className, /lv-cap--dim/, "下一句没压暗");
});

await check("落后多少秒优先问播放器, 别自己去读 seekable", async () => {
  /* hls.js 走 MSE, seekable 常常只反映**已缓冲范围** —— 播放器明明落后十几秒,
     它也能算出接近 0, 拿它对字幕等于没对 (创始人第三次报"对不齐"的根子)。
     播放器那边能拿到 hls.latency, 那才是距直播边缘的真实延迟。 */
  const dom = makeDom();
  setLag(dom, 0);                       // seekable 说"我贴着边缘"
  dom.window.LivePlayer = { lag: () => 26 };   // 播放器说"其实落后 26 秒"
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: FIVE });
  await api.pull();
  assert.equal(nowLine(dom), "第1句",
    "还是信了 seekable —— 播放器报的 26 秒延迟被忽略, 字幕会比声音早一句半");
});

/* ── 按视频时间对齐 ────────────────────────────────────────────────────────
 *
 * 2026-09-10 创始人第三次报"字幕对不上"。这次的根因不在客户端算式, 而在于产出侧
 * 当天加了**墙钟节流**(把 1.65× 的产能压回 1.0×): 派单不再紧跟在上一句生成结束
 * 之后, 中间多了 3.5~8 秒的刹车等待。而墙钟那条算法整个建立在"派单≈上一句结束"
 * 之上(所以它要往回退一句)。前提没了, 补偏移量也治不了 —— 等待长度随负载变。
 *
 * 解法是让服务端记**物理量**: 每句的视频从整条流的第几秒开始 (vt)。
 */
// 五句, 每句 9 秒视频 —— vt 是它在整条流里的起点
const VT5 = [0, 9, 18, 27, 36].map((vt, i) =>
  ({ vt, t: 0, kind: "script", text: "第" + (i + 1) + "句" }));

console.log("字幕按视频时间对齐:");

await check("vt 直接给出正在播的那一句 —— 没有「往回退一句」的修正", async () => {
  const dom = makeDom();
  const api = load(dom);
  // 边缘播到第 45 秒, 观众落后 12 秒 -> 33 秒 -> 落在第 4 句 (vt 区间 27~36)
  assert.equal(api.pickVt(VT5, 45, 0, 12), 3);
  // 贴着边缘就是最后一句 —— 墙钟那条在这里要退一句, vt 这条不退
  assert.equal(api.pickVt(VT5, 45, 0, 0), 4);
});

await check("轮询延迟要往前推 —— 观众永远按 1.0× 走, 与产出快慢无关", async () => {
  const dom = makeDom();
  const api = load(dom);
  assert.equal(api.pickVt(VT5, 45, 0, 12), 3);
  assert.equal(api.pickVt(VT5, 45, 9, 12), 4, "过了九秒该进下一句");
});

await check("⛔ 节流把派单时间戳推开之后, 墙钟那条必然算错 —— 这就是要 vt 的原因", async () => {
  const dom = makeDom();
  const api = load(dom);
  // 节流稳态: 产出领先墙钟 4 秒, 所以第 k 句在 t = 9k - 4 派出去
  const paced = [-4, 5, 14, 23, 32].map((t, i) =>
    ({ t, vt: VT5[i].vt, kind: "script", text: VT5[i].text }));
  const byWall = api.pick(paced, 32, 0, 12);
  const byVt = api.pickVt(paced, 45, 0, 12);
  assert.equal(byVt, 3, "视频时间该落在第 4 句");
  assert.notEqual(byWall, byVt,
    "墙钟那条居然也对了 —— 这个用例没复现出问题, 说明构造的时间戳不对");
  assert.equal(byWall, 1, `墙钟那条落在第 ${byWall + 1} 句, 比声音早了两句`);
});

await check("端到端: 载荷带 vt 和 edge 时走视频时间那条", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  setLag(dom, 12);
  dom.set({ live: true, edge: 45, lines: VT5 });
  await api.pull();
  assert.equal(nowLine(dom), "第4句");
});

await check("上游还没升级(缺 vt/edge)要回落到墙钟那条 —— 客户端才能先发", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick();
  dom.set({ live: true, lines: FIVE });     // 没有 vt, 也没有 edge
  await api.pull();
  assert.equal(nowLine(dom), "第4句", "缺 vt 时没回落到墙钟算法, 字幕会整个飞掉");
});
