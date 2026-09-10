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
    // 首选播放器自己报的 —— 它能拿到 hls.latency, 那是距直播边缘的真实延迟。
    // seekable 在 MSE 下只反映已缓冲范围, 播放器落后十几秒时也能算出接近 0。
    var P = window.LivePlayer;
    if (P && typeof P.lag === 'function') {
      var l = P.lag();
      if (isFinite(l) && l >= 0) return l;
    }
    if (!video || !video.seekable || !video.seekable.length) return 0;
    var edge = video.seekable.end(video.seekable.length - 1);
    var cur = video.currentTime;
    if (!isFinite(edge) || !isFinite(cur)) return 0;
    var l2 = edge - cur;
    if (!(l2 > 0)) return 0;
    return l2 > 120 ? 120 : l2;
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

  /* 按**视频时间**挑正在播的那一句。**纯函数, 给测试用。**
   *
   * 为什么要有它: 上面那条 pick() 按墙钟算, 它依赖"派单紧跟在上一句生成结束之后"
   * (队列里压着 QUEUE_AHEAD 句, 所以要往回退一句)。2026-09-10 产出侧加了墙钟节流
   * 把产出压回 1.0×, 派单和上一句结束之间多了 3.5~8 秒的刹车等待 —— 那个前提没了,
   * 字幕就对不上(创始人当天第三次报)。**补一个偏移量治不了本**: 等待长度随机器
   * 负载变, 邻居一抢 CPU 就又错开。
   *
   * vt 是物理量: 这一句的视频从整条流的第几秒开始 (服务端在数字人 `begin` 时用
   * 当时的时间轴偏移记的, 见 live_server 的 begin 分支)。观众此刻播到第几秒:
   *     viewerVt = 直播边缘的视频秒数 - 落后秒数 + 收到这批数据之后过去的时间
   * 观众永远按 1.0× 走, 与产出快慢无关, 所以这条外推是准的。落在哪一句的区间里
   * 就显示哪一句 —— **不用往回退一句**, vt 说的就是"这句从哪开始"。
   */
  function pickVt(lines, edgeVt, elapsed, lag) {
    var n = lines.length;
    if (!n) return -1;
    var target = edgeVt - lag + elapsed;
    var i = -1;
    for (var k = 0; k < n; k++) {
      if (lines[k].vt <= target) i = k; else break;
    }
    return i < 0 ? 0 : i;               // 全都比 target 新: 退到最早的一句
  }

  /* 上一句 / 正在说 / 下一句 —— 共三行, 中间那句加重。
   *
   * 第一版堆最近十几句, 占满右侧还盖住了声音按钮; 第二版只留一句, 但中途进来的
   * 观众看不到上下文。三行是折中: 有上下文, 又不会长到盖住底下的按钮
   * (.lv-caps 底部留了 56px 给它, 见 live.css)。 */
  function render(lines, idx) {
    var cur = lines[idx];
    var id = idx + '|' + (cur ? cur.t : '') + '|' + (cur ? cur.text : '');
    if (id === shownId) return;
    shownId = id;
    box.textContent = '';
    [idx - 1, idx, idx + 1].forEach(function (i) {
      var line = lines[i];
      if (!line || !line.text) return;
      var el = document.createElement('div');
      el.className = 'lv-cap' + (i === idx ? ' lv-cap--now' : ' lv-cap--dim')
        + (line.kind === 'interject' ? ' lv-cap--in' : '');
      el.textContent = line.text;
      box.appendChild(el);
    });
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
    var elapsed = nowSec() - snap.at, lag = lagBehindEdge();
    // 服务端给了视频时间就走 vt —— 那条不依赖队列深度和节流。给不出(老版本上游)
    // 才回落到墙钟那条, 所以客户端可以先发, 不会因为上游还没升级而破。
    var idx = snap.hasVt
      ? pickVt(snap.lines, snap.edgeVt, elapsed, lag)
      : pick(snap.lines, snap.edgeT, elapsed, lag);
    if (idx >= 0) render(snap.lines, idx);
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
        // 全部句子都带 vt 且边缘也给了, 才走视频时间那条 —— 半套数据比没有更危险。
        var hasVt = isFinite(d.edge) && ls.every(function (x) { return isFinite(x.vt); });
        snap = { lines: ls, edgeT: edgeT, edgeVt: +d.edge, at: nowSec(), hasVt: hasVt };
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
    pickVt: pickVt,
    stop: function () { clearInterval(timer); clearInterval(ticker); },
  };
})();
