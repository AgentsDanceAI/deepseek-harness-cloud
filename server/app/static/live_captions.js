/* 直播字幕: 画面右侧半透明滚动条。观看页与控制台共用。
 *
 * 为什么要有它: 观众可能静音看 (地铁上、办公室里), 也可能中途进来 —— 没有字幕
 * 就完全不知道她在说什么, 而这是一场"她一直在说话"的直播。
 *
 * 两个刻意的选择:
 *  1. 只换不堆。屏幕上永远只有正在说的那一句 —— 第一版把最近十几句堆在右边滚动,
 *     结果三分之一画面被字幕占满, 而且盖住了声音按钮。
 *  2. 服务端已经缓存了两秒 (见 live.py 的 _CAP_CACHE), 所以三秒才问一次:
 *     一百个观众打到 GPU 上的仍然是每两秒一次。字幕的推进不靠轮询, 靠本地
 *     半秒一次的 tick 在已有数据上重算 (见下面的时间轴对齐)。
 */
window.LiveCaptions = (function () {
  var video = document.getElementById('lvVideo');
  var stage = video && video.parentNode;
  var box = document.getElementById('lvCaps');
  var toggle = document.getElementById('lvCapsToggle');
  if (!box || !stage) return null;

  var snap = null;        // 最近一次拿到的字幕表 + 收到它的本地时刻
  var shownId = '';       // 当前屏幕上那一句的身份 (允许回退到更早的句子)
  var timer = null, ticker = null;
  var KEY = 'dhc.live.captions';

  function nowSec() { return Date.now() / 1000; }

  function on() {
    try { return localStorage.getItem(KEY) !== 'off'; } catch (e) { return true; }
  }
  function paintToggle() {
    box.hidden = !on();
    if (toggle) {
      toggle.textContent = toggle.dataset[on() ? 'hide' : 'show'] || '';
      toggle.setAttribute('aria-pressed', on() ? 'true' : 'false');
    }
  }

  /* 观众落后直播边缘多少秒。
   *
   * 这个数**不是小数点后的修饰**: 播放器是**故意**退后十几秒的
   * (live.js 的 liveSyncDurationCount=12; 卡顿恢复还会退到 BEHIND_LIVE=10),
   * 而一句话约 8-9 秒 —— 也就是说观众听到的比直播边缘晚一句半。 */
  function lagBehindEdge() {
    if (!video || !video.seekable || !video.seekable.length) return 0;
    var edge = video.seekable.end(video.seekable.length - 1);
    var cur = video.currentTime;
    if (!isFinite(edge) || !isFinite(cur)) return 0;
    var l = edge - cur;
    if (!(l > 0)) return 0;
    return l > 120 ? 120 : l;      // 离谱值当没有, 别把字幕甩到几分钟前
  }

  /* 挑出"此刻正在播"的那一句。**纯函数, 给测试用。**
   *
   * 时间轴是这么对上的:
   *   上游在句子**发给数字人时**记账 (live_server 的 feed 里), 而队列里始终压着
   *   QUEUE_AHEAD=2 句。发第 j 句这个动作紧跟在第 j-2 句生成结束之后, 所以
   *   `t[j]` ≈ 第 j-2 句抵达直播边缘的时刻 —— 于是第 k 句占据边缘的时间区间是
   *   [t[k+1], t[k+2])。
   *   观众落后边缘 lag 秒, 此刻看的是边缘在 `T = 服务端此刻 - lag` 时的内容,
   *   于是: 取满足 t[i] <= T 的最大 i, 正在播的就是第 i-1 句。
   *
   *   "服务端此刻" = 这批数据里最新的 t + 收到它之后过去的时间 —— 这样轮询本身
   *   的三五秒延迟不会被算进 lag 里 (算进去会让字幕整体偏后一句)。
   *
   * ⚠️ lag=0 时结果正好是倒数第二条, 与上一版一致 —— 上一版的错不在这个式子,
   * 而在于它把 lag 当成了 0, 可播放器从来不贴着边缘走。 */
  function pick(lines, edgeT, elapsed, lag) {
    var n = lines.length;
    if (!n) return -1;
    if (n === 1) return 0;                    // 刚开播只有一句, 显示它
    var target = edgeT + elapsed - lag;
    var i = -1;
    for (var k = 0; k < n; k++) {
      if (lines[k].t <= target) i = k; else break;
    }
    if (i < 0) return 0;                      // 全都比 target 新: 退到最早的一句
    var idx = i - 1;
    if (idx < 0) idx = 0;
    if (idx > n - 1) idx = n - 1;
    return idx;
  }

  function render(line) {
    var id = line.t + '|' + line.text;
    if (id === shownId) return;
    shownId = id;
    var el = document.createElement('div');
    el.className = 'lv-cap' + (line.kind === 'interject' ? ' lv-cap--in' : '');
    el.textContent = line.text;
    box.textContent = '';        // 换掉上一句, 不堆叠
    box.appendChild(el);
  }

  function clear() {
    box.textContent = '';
    shownId = '';
    snap = null;
  }

  /* 在**已有**数据上重算该显示哪一句。半秒一次, 不发请求。
     字幕的推进靠这里而不是靠轮询 —— 三秒一问的话, 换句的时刻最多能差三秒。 */
  function tick() {
    if (!snap || !snap.lines.length) return;
    var idx = pick(snap.lines, snap.edgeT, nowSec() - snap.at, lagBehindEdge());
    if (idx >= 0) render(snap.lines[idx]);
  }

  function pull() {
    return fetch('/api/live/captions', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (!d.live) { clear(); return; }   // 停播就清空, 别留上一场的字
        var ls = (d.lines || []).filter(function (x) { return x && x.text; });
        if (!ls.length) return;
        var edgeT = 0;
        for (var i = 0; i < ls.length; i++) if (ls[i].t > edgeT) edgeT = ls[i].t;
        snap = { lines: ls, edgeT: edgeT, at: nowSec() };
        tick();
      })
      .catch(function () {});
  }

  if (toggle) {
    toggle.addEventListener('click', function () {
      try { localStorage.setItem(KEY, on() ? 'off' : 'on'); } catch (e) { /* 无痕窗口 */ }
      paintToggle();
    });
  }
  paintToggle();
  pull();
  timer = setInterval(pull, 3000);
  ticker = setInterval(tick, 500);
  return {
    pull: pull,
    pick: pick,                  // 给测试用 —— 时间轴对齐是这一页唯一容易算错的地方
    stop: function () { clearInterval(timer); clearInterval(ticker); },
  };
})();
