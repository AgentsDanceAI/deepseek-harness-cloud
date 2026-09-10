/* 直播播放器: 真的跑 live.js 的状态机。
 *
 * 这三条都是创始人在线上撞到的:
 *   · 停播之后再开播"半天没反应"  —— 地址没变, 守卫把重建挡掉了
 *   · 开了声音就关不掉            —— 按钮是单向的
 *   · 有时候打开是黑屏            —— 自动播放被拒, 而拒绝是静默的
 *
 * 跑法: node server/tests/js/live_player.test.mjs
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "app", "static", "live.js");

function makeDom({ nativeHls = false, autoplayBlocked = false, blockedWhenUnmuted = false } = {}) {
  const state = { live: true, enabled: true };
  const gate = { blockUnmuted: blockedWhenUnmuted };
  const hlsInstances = [];
  const el = (id, extra = {}) => ({
    id, textContent: "", hidden: false, className: "", dataset: {}, style: {},
    listeners: {}, parentNode: null,
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); },
    removeEventListener(t, fn) {
      this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn);
    },
    fire(t, ev = {}) { (this.listeners[t] || []).forEach((f) => f(ev)); },
    setAttribute(k, val) { this.attrs = this.attrs || {}; this.attrs[k] = val; },
    removeAttribute(k) { if (k === "src") this.src = ""; },
    ...extra,
  });

  const stage = el("stage");
  const video = el("lvVideo", {
    muted: true, volume: 0, paused: true, src: "",
    loads: 0, plays: 0,
    canPlayType: () => (nativeHls ? "maybe" : ""),
    play() {
      this.plays++;
      // 浏览器的真实行为: **静音**自动播放放行, 非静音的自动播放被拒。
      // ⚠️ 用户手势(点按钮)触发的非静音播放是**放行**的 —— 所以这道闸要能中途切换,
      // 一直开着的话连"点开声音"都会被拒, 测的就不是真实浏览器了。
      if (autoplayBlocked || (gate.blockUnmuted && !this.muted)) {
        return Promise.reject(new Error("NotAllowedError"));
      }
      this.paused = false;
      return Promise.resolve();
    },
    pause() { this.paused = true; },
    load() { this.loads++; },
  });
  video.parentNode = stage;

  const badge = el("lvBadge");
  badge.dataset = {
    on: "直播中", off: "未开播", notlive: "主播暂时不在", disabled: "未开通",
    reconnect: "重连中", unsupported: "不支持", mute: "关声音", tapplay: "点一下开始播放",
  };
  // live.js 用 t('unmute') 取按钮文案, 而 unmute 这一条在观看页上是按钮自己的初始
  // 文本; badge 上没有 —— 补一条, 否则切回静音时文案是空的。
  badge.dataset.unmute = "开声音";
  badge.dataset.remuted = "重连后浏览器挡住了声音，点「开声音」恢复";

  const els = { "#lvVideo": video, "#lvMsg": el("lvMsg"), "#lvBadge": badge, "#lvUnmute": el("lvUnmute") };
  const byId = { lvVideo: video, lvMsg: els["#lvMsg"], lvBadge: badge, lvUnmute: els["#lvUnmute"] };

  const document = {
    getElementById: (id) => byId[id] || null,
    addEventListener() {},
    hidden: false,
  };
  const timers = [];
  const window = {
    Hls: class {
      static isSupported() { return true; }
      constructor(cfg) { this.cfg = cfg; this.destroyed = false; this.loaded = ""; hlsInstances.push(this); }
      loadSource(u) { this.loaded = u; }
      attachMedia() {}
      on(ev, fn) { (this.handlers ||= {})[ev] = fn; }
      startLoad() { this.restarted = true; }
      recoverMediaError() {}
      destroy() { this.destroyed = true; }
    },
    fetch: () => Promise.resolve({ json: () => Promise.resolve({
      enabled: state.enabled, live: state.live, hls: "/api/live/hls/official/index.m3u8",
    }) }),
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    // 卡死恢复那一路是 1 秒一拍, 状态刷新是 15 秒 —— 按周期挑, 别按顺序猜
    __tick: (ms, n) => {
      const t = timers.filter((x) => x.ms === ms);
      for (let i = 0; i < n; i++) t.forEach((x) => x.fn());
    },
  };
  window.Hls.Events = { MANIFEST_PARSED: "mp", ERROR: "err" };
  window.Hls.ErrorTypes = { NETWORK_ERROR: "net", MEDIA_ERROR: "media" };
  return { document, window, els, video, badge, stage, state, hlsInstances, timers, gate };
}

function load(dom) {
  const src = readFileSync(SRC, "utf8");
  const fn = new Function("document", "window", "fetch", "setInterval", "setTimeout", src);
  fn(dom.document, dom.window, dom.window.fetch, dom.window.setInterval, () => 0);
  return dom.window.LivePlayer;
}

const tick = () => new Promise((r) => setImmediate(r));

async function check(name, fn) {
  try { await fn(); console.log("  ✓", name); }
  catch (e) { console.log("  ✗", name, "\n     ", e.message); process.exitCode = 1; }
}

console.log("直播播放器:");

await check("停播 -> 再开播: 播放器必须重建, 不能被'地址没变'挡掉", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick(); await tick();
  assert.equal(dom.hlsInstances.length, 1, "第一次开播没起播");

  dom.state.live = false;                 // 主播停了
  await api.refresh(); await tick();
  assert.equal(dom.hlsInstances[0].destroyed, true, "停播了还留着 hls 实例");

  dom.state.live = true;                  // 又开播了 —— 地址与上次**完全一样**
  await api.refresh(); await tick();
  assert.equal(dom.hlsInstances.length, 2,
    "没有重建播放器 —— 这就是'停播之后开播半天没反应', 页面一切正常就是黑着");
});

await check("Safari 原生 HLS 那条路也要重建", async () => {
  const dom = makeDom({ nativeHls: true });
  const api = load(dom);
  await tick(); await tick();
  const first = dom.video.loads;

  dom.state.live = false; await api.refresh(); await tick();
  dom.state.live = true;  await api.refresh(); await tick();
  assert.ok(dom.video.loads > first, "Safari 上停播再开播没有重新加载");
});

await check("声音是开关: 点两次回到静音, 按钮一直在", async () => {
  const dom = makeDom();
  load(dom);
  await tick(); await tick();
  dom.video.fire("playing");
  const btn = dom.els["#lvUnmute"];
  assert.equal(btn.hidden, false, "起播了却没有声音按钮");
  assert.equal(dom.video.muted, true);

  btn.fire("click");
  assert.equal(dom.video.muted, false, "点了没开声音");
  assert.equal(btn.hidden, false, "开完声音按钮就消失了 —— 想关也关不掉");

  btn.fire("click");
  assert.equal(dom.video.muted, true, "第二次点没有静音回去");
});

await check("自动播放被拒 -> 说话, 并且点画面能播", async () => {
  const dom = makeDom({ autoplayBlocked: true });
  load(dom);
  await tick(); await tick();
  const inst = dom.hlsInstances[0];
  inst.handlers.mp();                      // MANIFEST_PARSED -> 尝试起播 -> 被拒
  await tick();
  assert.equal(dom.els["#lvMsg"].textContent, "点一下开始播放",
    "自动播放被拒却一声不吭 —— 观众看到的就是纯黑一片");

  const before = dom.video.plays;
  dom.stage.fire("click");
  assert.ok(dom.video.plays > before, "点了画面还是不播");
});

await check("未开播时也要把播放器收掉, 别留着最后一帧", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick(); await tick();
  dom.state.live = false;
  await api.refresh(); await tick();
  assert.equal(dom.els["#lvMsg"].textContent, "主播暂时不在");
  assert.equal(dom.hlsInstances[0].destroyed, true);
});

await check("卡住 6 秒 -> 自己跳回直播边缘 (hls.js)", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick(); await tick();
  const inst = dom.hlsInstances[0];
  inst.liveSyncPosition = 120;
  dom.video.paused = false;
  dom.video.currentTime = 5;          // 时间停在这儿不动了

  // 第一拍只是记基准, 所以"6 秒不动"要 7 拍才成立
  dom.window.__tick(1000, 6);
  assert.equal(dom.video.currentTime, 5, "不到 6 秒就跳了 —— 会误伤正常抖动");
  dom.window.__tick(1000, 1);
  assert.equal(dom.video.currentTime, 120, "卡了 6 秒还不跳 —— 观众只能自己刷新");
  assert.equal(inst.restarted, true, "没有重新拉流");
});

await check("Safari 原生那条路也要能恢复", async () => {
  const dom = makeDom({ nativeHls: true });
  const api = load(dom);
  await tick(); await tick();
  dom.video.paused = false;
  dom.video.currentTime = 3;
  dom.video.seekable = { length: 1, end: () => 300 };
  dom.window.__tick(1000, 8);
  // 退到边缘**之后** 10 秒, 不是贴着边缘 —— 贴着边恢复的话 0.9× 的产出几秒钟
  // 就又抽干, 变成每几秒跳一次, 比一直卡着还难看。
  assert.ok(dom.video.currentTime > 280 && dom.video.currentTime <= 291,
    `Safari 恢复位置不对 (currentTime=${dom.video.currentTime}), 应该在 edge-10 附近`);
});

await check("缓冲配到 12 秒 —— 0.9× 产出下这决定多久见底", () => {
  const src = readFileSync(SRC, "utf8");
  const m = /liveSyncDurationCount:\s*(\d+)/.exec(src);
  assert.ok(m, "找不到 liveSyncDurationCount");
  const n = +m[1];
  assert.ok(n >= 12, `缓冲只有 ${n} 秒 —— 0.9× 产出下约 ${(n / 0.1 / 60).toFixed(1)} 分钟就见底`);
  const mx = /liveMaxLatencyDurationCount:\s*(\d+)/.exec(src);
  assert.ok(mx && +mx[1] > n, "liveMaxLatencyDurationCount 必须大于 liveSyncDurationCount");
});

await check("正常播放时绝不乱跳", async () => {
  const dom = makeDom();
  const api = load(dom);
  await tick(); await tick();
  dom.hlsInstances[0].liveSyncPosition = 999;
  dom.video.paused = false;
  for (let i = 0; i < 20; i++) { dom.video.currentTime = i; dom.window.__tick(1000, 1); }
  assert.ok(dom.video.currentTime < 100, "播得好好的却被跳到直播边缘");
});

await check("重连后非静音被拒: 退回静音播放, **不能把画面变成一块黑屏**", async () => {
  /* 管理员在控制台按一次保存, live_server 就把播出停掉重开 (话术即时生效的代价,
     见它的 put_room)。观众这边 teardown 之后重连, 而元素已经被观众解除过静音 ——
     浏览器不放行非静音的自动播放。
     旧写法直接挂"点一下开始播放": 观众看到的是**画面没了**, 只会以为直播挂了。 */
  const dom = makeDom();
  const api = load(dom);
  await tick(); await tick();
  dom.hlsInstances[0].handlers.mp();             // 起播
  await tick();
  dom.video.fire("playing");                     // 声音按钮出现
  dom.els["#lvUnmute"].fire("click");            // 观众点开声音 (有手势, 浏览器放行)
  await tick();
  assert.equal(dom.video.muted, false, "点了开声音却还是静音的");

  dom.gate.blockUnmuted = true;                  // 此后的**自动**播放才会被拒
  dom.state.live = false;                        // 保存 -> 停播
  await api.refresh(); await tick();
  dom.state.live = true;                         // -> 重开
  await api.refresh(); await tick();
  dom.hlsInstances[1].handlers.mp();             // 新实例起播 -> 非静音被拒
  await tick(); await tick();

  assert.equal(dom.video.paused, false,
    "画面停住了 —— 观众看到的是直播挂了, 而其实只是保存了一次配置");
  assert.equal(dom.video.muted, true, "没有退回静音, 那这次 play() 根本没成功");
  assert.match(dom.els["#lvMsg"].textContent, /开声音/,
    "没告诉观众声音被挡了, 他不知道点哪儿能拿回来");
  assert.ok(!/点一下开始播放/.test(dom.els["#lvMsg"].textContent),
    "还是挂出了'点一下开始播放' —— 画面本可以是连着的");
});

await check("被迫静音之后, 点「开声音」要能把声音拿回来", async () => {
  /* 这是这次改动真正的风险: 我们**程序性地**把 muted 设回了 true。要是按钮的状态
     跟不上, 观众就卡在一个"有声音按钮但按了没用"的地方 —— 比黑屏更让人火大。 */
  const dom = makeDom();
  const api = load(dom);
  await tick(); await tick();
  dom.hlsInstances[0].handlers.mp();
  await tick();
  dom.video.fire("playing");
  dom.els["#lvUnmute"].fire("click");
  await tick();

  dom.gate.blockUnmuted = true;
  dom.state.live = false; await api.refresh(); await tick();
  dom.state.live = true;  await api.refresh(); await tick();
  dom.hlsInstances[1].handlers.mp();
  await tick(); await tick();
  assert.equal(dom.video.muted, true, "前置条件不成立: 没被迫静音");
  assert.equal(dom.els["#lvUnmute"].textContent, "开声音",
    "被迫静音后按钮文案没跟上 —— 上面写着'关声音'而其实是静音的");

  dom.gate.blockUnmuted = false;                 // 观众这次点是有手势的
  dom.els["#lvUnmute"].fire("click");
  await tick();
  assert.equal(dom.video.muted, false, "点了开声音拿不回声音 —— 播放器卡死在静音里了");
  assert.equal(dom.video.paused, false, "拿回声音的同时把画面停了");
});
