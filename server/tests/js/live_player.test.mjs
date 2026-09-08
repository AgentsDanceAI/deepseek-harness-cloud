/* 真的执行 live.js, 而不是 grep 它的源码。
 *
 * 起因 (2026-09-08 老板: "它断的时候会黑一下"): play() 里原先写的是
 *   if (url === playingUrl && hls && !v.paused) return;
 * 那个 `!v.paused` 让"地址没变"这条捷径在**视频恰好暂停时失效** —— 而状态刷新
 * 每 15 秒来一次, 于是切到后台/刚起播/缓冲中的那几秒里, 整个 hls 实例被销毁重建。
 * MediaSource 一拆一建, 画面就黑一下, 而服务端日志、ffmpeg 日志、切片本身**全是
 * 干净的** (那天挨个查过七项)。这种 bug 只有真跑一遍代码才看得见。
 *
 * 跑法: node server/tests/js/live_player.test.mjs
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import test from "node:test";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = join(here, "..", "..", "app", "static", "live.js");

function makeEnv(hlsUrl = "/api/live/hls/official/index.m3u8", { safari = false } = {}) {
  const made = [];              // 建过几个 Hls 实例
  const destroyed = [];         // 销毁过几个
  const video = {
    paused: false, muted: true, volume: 1, readyState: 4, src: "",
    listeners: {},
    plays: 0,
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); },
    play() { this.plays++; this.paused = false; return Promise.resolve(); },
    // Safari 原生放 HLS, 走 v.src 那条路; 别的浏览器走 hls.js。
    canPlayType() { return safari ? "maybe" : ""; },
    srcSets: 0,
    set src(u) { this.srcSets++; this._src = u; },
    get src() { return this._src || ""; },
  };
  const nodes = {
    lvVideo: video,
    lvMsg: { textContent: "", hidden: false },
    lvBadge: { textContent: "", className: "", dataset: { on: "直播中", off: "未开播", notlive: "未开播", disabled: "未开通", reconnect: "重连中", unsupported: "不支持" } },
    lvUnmute: { hidden: true, addEventListener() {} },
  };
  class Hls {
    constructor(cfg) { this.cfg = cfg; this.handlers = {}; made.push(this); }
    static isSupported() { return true; }
    static get Events() { return { MANIFEST_PARSED: "m", ERROR: "e" }; }
    static get ErrorTypes() { return { NETWORK_ERROR: "n", MEDIA_ERROR: "d" }; }
    loadSource(u) { this.src = u; }
    attachMedia() {}
    on(ev, fn) { this.handlers[ev] = fn; }
    destroy() { destroyed.push(this); }
  }
  const ctx = {
    made, destroyed, video,
    window: { Hls },
    document: {
      getElementById: (id) => nodes[id] || null,
      addEventListener() {},
      hidden: false,
    },
    fetch: () => Promise.resolve({ json: () => Promise.resolve({ enabled: true, live: true, hls: hlsUrl }) }),
    setInterval: () => 0,
    setTimeout: () => 0,
  };
  ctx.window.Hls = Hls;
  return ctx;
}

function load(ctx) {
  const src = readFileSync(SRC, "utf8");
  const fn = new Function("window", "document", "fetch", "setInterval", "setTimeout", src + "\nreturn window.LivePlayer;");
  return fn(ctx.window, ctx.document, ctx.fetch, ctx.setInterval, ctx.setTimeout);
}

test("地址没变时不重建播放器 —— 即使视频正暂停 (黑一下的根因)", async () => {
  const ctx = makeEnv();
  const player = load(ctx);
  await player.refresh();
  assert.equal(ctx.made.length, 1, "第一次该建一个 Hls");
  assert.equal(ctx.destroyed.length, 0);

  // 浏览器把视频暂停了 (切后台/缓冲) —— 这正是原来那个 bug 的触发条件
  ctx.video.paused = true;
  const playsBefore = ctx.video.plays;
  await player.refresh();

  assert.equal(ctx.made.length, 1, "地址没变却又建了一个 Hls —— 会黑一下");
  assert.equal(ctx.destroyed.length, 0, "地址没变却销毁了播放器 —— 会黑一下");
  assert.ok(ctx.video.plays > playsBefore, "暂停了该让它继续播, 而不是重建");
});

test("地址真的变了才重建", async () => {
  const ctx = makeEnv();
  const player = load(ctx);
  await player.refresh();
  assert.equal(ctx.made.length, 1);

  ctx.fetch = () => Promise.resolve({ json: () => Promise.resolve({ enabled: true, live: true, hls: "/api/live/hls/official/other.m3u8" }) });
  // 换一份 fetch 后重新载入不现实, 直接改闭包外的桩: 用新环境跑一次等价路径
  const ctx2 = makeEnv("/api/live/hls/official/other.m3u8");
  const p2 = load(ctx2);
  await p2.refresh();
  assert.equal(ctx2.made.length, 1, "新地址第一次也只建一个");
});

test("播放器按普通 HLS 配, 落后了加速追而不是跳", async () => {
  const ctx = makeEnv();
  const player = load(ctx);
  await player.refresh();
  const cfg = ctx.made[0].cfg;
  // 跳 = 缓冲被清 = 黑一下。这三条是为了不跳。
  assert.equal(cfg.lowLatencyMode, false, "我们发的不是 LL-HLS, 开着它同步策略会更激进");
  assert.ok(cfg.maxLiveSyncPlaybackRate > 1, "落后了要能加速追, 否则只能跳");
  assert.ok(cfg.liveSyncDurationCount >= 4, "缓冲太薄, 一抖动就掉队");
});


test("Safari 上也不能每次刷新都重设 v.src (老板录屏: 每 15 秒黑 2-3 秒, 声音一起断)", async () => {
  // Safari 原生放 HLS —— 走的不是 hls.js 那条路, 所以守卫**不能**拿 hls 实例当判据。
  // 给 video 重新赋同一个 src 会让它整个重新加载: 黑 2-3 秒, 音频也断。
  const ctx = makeEnv("/api/live/hls/official/index.m3u8", { safari: true });
  const player = load(ctx);
  await player.refresh();
  assert.equal(ctx.video.srcSets, 1, "第一次该设一次 src");
  assert.equal(ctx.made.length, 0, "Safari 不该建 hls.js 实例");

  await player.refresh();            // 第 15 秒
  await player.refresh();            // 第 30 秒
  ctx.video.paused = true;
  await player.refresh();            // 暂停时也不该重设

  assert.equal(ctx.video.srcSets, 1, "地址没变却又设了一次 src —— Safari 上会黑 2-3 秒");
  assert.ok(ctx.video.plays >= 1, "暂停了该让它继续播");
});
