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
      enabled: state.enabled, live: state.live, since: state.since,
      hls: "/api/live/hls/official/index.m3u8",
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

/* 观众落后直播边缘多少秒。2026-09-10 产出侧加了墙钟节流(把 1.65× 压回 1.0×)之后,
   产出变成锯齿: 句内每 0.5~1.1 秒出片, **句间有 5~8 秒空档**(实测 75 秒内七次,
   最大 8.0 秒) —— 那是压回 1.0× 的固有代价, 不是 bug。
   落后不够多, 空档一来缓冲就见底: 画面停、没声音, 过几秒又恢复(线上真出过, 当时是 7 秒)。 */
const MAX_GAP = 8;      // 句间最大空档, 实测值
await check("缓冲必须大于句间空档 —— 小了就是「播一会儿没声音」", () => {
  const src = readFileSync(SRC, "utf8");
  const m = /liveSyncDuration:\s*(\d+)/.exec(src);
  assert.ok(m, "找不到 liveSyncDuration (注意: 不是 Count —— count 要乘 TARGETDURATION)");
  const n = +m[1];
  assert.ok(n > MAX_GAP,
    `落后只有 ${n} 秒, 而句间空档最大 ${MAX_GAP} 秒 —— 缓冲会见底, 观众听到的是断断续续`);
  const mx = /liveMaxLatencyDuration:\s*(\d+)/.exec(src);
  assert.ok(mx && +mx[1] > n, "liveMaxLatencyDuration 必须大于 liveSyncDuration");
  const behind = /BEHIND_LIVE\s*=\s*(\d+)/.exec(src);
  assert.ok(behind && +behind[1] === n,
    `Safari 那条路的 BEHIND_LIVE=${behind && behind[1]} 与 liveSyncDuration=${n} 不一致 —— 两处要一起改`);
});

await check("不能开变速追赶 —— 锯齿会让语速每十秒忽快忽慢", () => {
  const src = readFileSync(SRC, "utf8");
  const m = /maxLiveSyncPlaybackRate:\s*([\d.]+)/.exec(src);
  assert.ok(m, "找不到 maxLiveSyncPlaybackRate");
  assert.equal(+m[1], 1,
    "开着变速追赶时, 每次产出爆发后落后变大就提速、空档里又降回来, 听感是一顿一顿的");
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

/* ── 换场必须重建播放器 ──────────────────────────────────────────────────────
 *
 * 切形象/音色时 live_server 的 put_room 是在**同一个请求里** stop+start, 所以客户端
 * 很可能从头到尾都看到 live:true —— 而播放列表已经被 rmtree 重建, MEDIA-SEQUENCE
 * 退回 0, 手里缓冲的切片全 404, 时间轴还倒退了。地址永远是同一个 index.m3u8,
 * play() 开头的 `url === playingUrl` 守卫会直接返回, 播放器就一直卡在废掉的 MSE
 * 缓冲上: **画面全黑, 点一下也没反应**(创始人 2026-09-10 切形象后撞到)。
 * 判据只能是 since(这一场的开播时刻)。
 */
console.log("换场:");

await check("切形象 (live 一直是 true, 地址没变) -> 播放器必须重建", async () => {
  const dom = makeDom();
  dom.state.since = 1000;
  const api = load(dom);
  await tick(); await tick();
  assert.equal(dom.hlsInstances.length, 1, "第一次开播没起播");
  dom.state.since = 2000;                       // 换了一场, 其余一切不变
  await api.refresh(); await tick();
  assert.equal(dom.hlsInstances[0].destroyed, true,
    "换场了却没拆掉旧实例 —— 它卡在废掉的缓冲上, 画面全黑且点了也没反应");
  assert.equal(dom.hlsInstances.length, 2, "没有重建播放器");
});

await check("同一场里反复轮询绝不能重建 —— 每重建一次就是一次黑屏", async () => {
  const dom = makeDom();
  dom.state.since = 1000;
  const api = load(dom);
  await tick(); await tick();
  for (let i = 0; i < 5; i++) { await api.refresh(); await tick(); }
  assert.equal(dom.hlsInstances.length, 1,
    `同一场被重建了 ${dom.hlsInstances.length} 次 —— since 没变就不该动`);
});

await check("上游给不出 since 时也不能乱拆 (老版本上游)", async () => {
  const dom = makeDom();
  const api = load(dom);                        // state.since 是 undefined
  await tick(); await tick();
  for (let i = 0; i < 3; i++) { await api.refresh(); await tick(); }
  assert.equal(dom.hlsInstances.length, 1, "没有 since 就该按兵不动, 而不是每轮都重建");
});

/* ── 点了还播不了 ───────────────────────────────────────────────────────────
 *
 * play() 被拒有两种完全不同的原因, 而以前一律当成第一种:
 *   · 没点过就播 —— 浏览器的自动播放策略, 挂个"点一下"就解决了;
 *   · **点了还被拒** —— 播放器废了(MSE 缓冲坏掉、切片全 404), 再挂"点一下"是死循环。
 * 创始人 2026-09-10 撞到: 18:10~18:16 连着七次"自动播放被拒", 每点一次又弹一次
 * (live_incidents 表记下的)。
 */
console.log("点了还播不了:");

await check("点击后仍被拒 -> 拆掉重建, 而不是再挂一次「点一下」", async () => {
  const dom = makeDom({ autoplayBlocked: true });
  const api = load(dom);
  await tick(); await tick();
  const inst = dom.hlsInstances[0];
  inst.handlers.mp();                      // MANIFEST_PARSED -> 起播 -> 被拒
  await tick();
  assert.equal(dom.els["#lvMsg"].textContent, "点一下开始播放", "没挂出「点一下」");
  assert.equal(inst.destroyed, false, "还没点就把播放器拆了");

  dom.stage.fire("click");                 // 点了 —— 但依旧被拒(播放器废了)
  await tick(); await tick(); await tick();
  assert.equal(inst.destroyed, true,
    "点了还播不了却没拆掉旧实例 —— 再点多少次都一样, 死循环");
});

await check("没点过的时候不能拆 —— 那只是自动播放策略, 挂「点一下」就够了", async () => {
  const dom = makeDom({ autoplayBlocked: true });
  load(dom);
  await tick(); await tick();
  dom.hlsInstances[0].handlers.mp();
  await tick(); await tick();
  assert.equal(dom.hlsInstances.length, 1, `起播就建了 ${dom.hlsInstances.length} 个实例`);
  assert.equal(dom.hlsInstances[0].destroyed, false,
    "自动播放被拒就把播放器拆了 —— 点一下本来能救回来的");
});

/* ── 卡住时该等还是该跳 ─────────────────────────────────────────────────────
 *
 * 跳是有代价的: 把已经缓冲的内容全丢掉, 而且本身就是一次可见的跳段。以前只要卡住
 * 6 秒就无条件跳, 结果是个**自我维持的循环**(2026-09-10 实测, live_incidents 记下的):
 *   产出空档耗干缓冲 -> 冻 6 秒 -> 跳 -> 跳把缓冲清空 -> 下一个空档立刻又见底 -> 再冻。
 *   (那批 waiting 事件的"落后"恒等于 12.0 —— 正是跳过去的落点, 指纹很清楚。)
 * 观众感受到的就是"一卡一卡"。
 */
console.log("卡住时该等还是该跳:");

await check("卡在缓冲空洞上 -> 推一小步跨过去, **不跳回同步点**", async () => {
  const dom = makeDom();
  load(dom);
  await tick(); await tick();
  const inst = dom.hlsInstances[0];
  inst.liveSyncPosition = 100;
  dom.video.paused = false;
  dom.video.currentTime = 95;         // 只落后同步点 5 秒 —— 还在窗口里
  // 播放头 95 卡在洞里, 洞后面 97.4 起有一整段缓冲
  dom.video.buffered = { length: 2, start: (i) => [90, 97.4][i], end: (i) => [95, 105][i] };
  dom.window.__tick(1000, 7);
  assert.notEqual(dom.video.currentTime, 100,
    "跳回同步点了 —— 缓冲被清空, 下一个产出空档马上又见底, 于是一卡一卡");
  assert.ok(dom.video.currentTime > 97.4 && dom.video.currentTime < 97.6,
    `没跨到洞后面那段缓冲 (currentTime=${dom.video.currentTime}) —— 固定步长推 0.3 秒` +
    "要卡好几分钟才跨得过去, 创始人报的「半天还没有说话」就是这个");
  assert.equal(inst.restarted, true, "连拉流都没催");
});

await check("洞后面根本没数据 -> 只能回同步点", async () => {
  const dom = makeDom();
  load(dom);
  await tick(); await tick();
  const inst = dom.hlsInstances[0];
  inst.liveSyncPosition = 100;
  dom.video.paused = false;
  dom.video.currentTime = 95;
  dom.video.buffered = { length: 1, start: () => 90, end: () => 95 };  // 前面没了
  dom.window.__tick(1000, 7);
  assert.equal(dom.video.currentTime, 100,
    "后面没数据还赖着不动 —— 会一直冻着");
});

await check("掉得太远 -> 还是要跳, 否则永远动不了", async () => {
  const dom = makeDom();
  load(dom);
  await tick(); await tick();
  const inst = dom.hlsInstances[0];
  inst.liveSyncPosition = 100;
  dom.video.paused = false;
  dom.video.currentTime = 60;         // 落后同步点 40 秒, 快掉出窗口了
  dom.window.__tick(1000, 7);
  assert.equal(dom.video.currentTime, 100, "掉这么远还不跳, 观众只能自己刷新");
});

await check("缓冲必须大于实测的最大空档 9.2 秒", () => {
  const src = readFileSync(SRC, "utf8");
  const n = +/liveSyncDuration:\s*(\d+)/.exec(src)[1];
  assert.ok(n > 9.2, `缓冲 ${n} 秒盖不住 9.2 秒的空档`);
  const behind = +/BEHIND_LIVE\s*=\s*(\d+)/.exec(src)[1];
  assert.equal(behind, n, "Safari 那条路的 BEHIND_LIVE 与 liveSyncDuration 不一致");
  const mx = +/liveMaxLatencyDuration:\s*(\d+)/.exec(src)[1];
  assert.ok(mx > n && mx < 38, `liveMaxLatencyDuration ${mx} 要在 ${n} 和窗长 38.6 之间`);
});

await check("上报要带上客户端版本 —— 否则「用户刷没刷新」只能靠猜", () => {
  const src = readFileSync(SRC, "utf8");
  assert.ok(/function clientVer/.test(src), "没有取客户端版本的地方");
  assert.ok(/\[v' \+ VER \+ '\]/.test(src) || /VER \+ '\]'/.test(src),
    "版本号没拼进上报的 detail 里");
  // 2026-09-10: 我按新参数分析了半天卡顿, 实际创始人跑的是旧 JS —— 指纹是
  // waiting 的"落后"恒等于旧的 liveSyncDuration。有版本号就不用靠这种间接推断。
});
