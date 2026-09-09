/* 直播播放器。控制台在 live_console.js 里 —— 这一份两个页面都用, 所以**不能**
 * 碰控制台那些元素 (观看页上它们根本不存在)。
 *
 * 两个坑:
 *  1. 自动播放策略只放行**静音**起播。所以 video 标签自带 muted, 出声靠按钮
 *     (那一下有用户手势)。不这么做的表现是整页安静地什么都不发生, 只有浏览器
 *     控制台里一行 NotAllowedError。
 *  2. 直播流会断 (切片轮转、网络抖动、上游重启)。hls.js 的致命错误必须自己接,
 *     否则播放器停在最后一帧, 而**看上去和"主播不说话"一模一样**。
 */
window.LivePlayer = (function () {
  var v = document.getElementById('lvVideo');
  var msg = document.getElementById('lvMsg');
  var badge = document.getElementById('lvBadge');
  var unmute = document.getElementById('lvUnmute');
  var hls = null, retry = 0, playingUrl = '';

  function t(k) { return (badge && badge.dataset[k]) || ''; }
  function note(s) { if (msg) { msg.textContent = s || ''; msg.hidden = !s; } }
  function online(on) {
    if (!badge) return;
    badge.textContent = t(on ? 'on' : 'off');
    badge.className = 'lv-badge lv-badge--' + (on ? 'on' : 'off');
  }

  /* 自动播放被拒时**必须说话**。静音自动播放大多放行, 但 iOS 低电量模式、部分
     浏览器的严格设置照样拒 —— 而拒绝是静默的: 画面纯黑, 提示已经被 note('') 清掉,
     控制台里只有一行 NotAllowedError。这就是"有时候打开黑屏"。
     接住之后把整块画面变成一个"点一下播放"的按钮 (.lv-msg 是 pointer-events:none,
     点击会落到舞台上)。 */
  var tapArmed = false;
  function tryPlay() {
    if (!v) return;
    var pr = v.play();
    if (pr && pr.catch) pr.catch(function () { note(t('tapplay')); armTap(); });
  }
  function armTap() {
    var stage = v && v.parentNode;
    if (!stage || tapArmed) return;
    tapArmed = true;
    stage.addEventListener('click', function once() {
      stage.removeEventListener('click', once);
      tapArmed = false;
      note('');
      tryPlay();
    });
  }

  function play(url) {
    if (!v) return;
    // ⚠️ 地址没变就**什么都别做**。判据只能是 playingUrl, 不能捎带别的条件 ——
    // 这一行前后错过两次:
    //   1. 原先写 `&& !v.paused`: 视频恰好暂停时守卫失效, 每 15 秒一次的状态刷新
    //      就把 hls 实例销毁重建;
    //   2. 改成 `&& hls` 之后在 **Safari 上仍然每 15 秒黑一次** —— Safari 原生放
    //      HLS, 走的是下面 `v.src = url` 那条路, `hls` 始终是 null, 于是守卫永远
    //      不生效, 每次刷新都把 v.src 重新赋一遍。给 video 重新赋同一个 src 会让
    //      它整个重新加载: 画面黑 2-3 秒, **声音一起断**。
    //      (老板的录屏实测: 黑 11.50-13.75s 与 26.75-29.75s, 相隔 15.25 秒。)
    // 暂停了该做的是让它继续播, 不是重来一遍。
    if (url === playingUrl) {
      if (v.paused) tryPlay();
      return;
    }
    playingUrl = url;
    if (hls) { hls.destroy(); hls = null; }
    if (v.canPlayType('application/vnd.apple.mpegurl')) {   // Safari 原生放 HLS
      v.src = url; tryPlay(); return;
    }
    if (!window.Hls || !window.Hls.isSupported()) { note(t('unsupported')); return; }
    hls = new window.Hls({
      // 我们发的是**普通** HLS (2 秒整片, 没有 EXT-X-PART)。开 lowLatencyMode 只会
      // 让它按低延迟那套去贴直播边缘, 落后一点就纠正 —— 而纠正的方式是**跳**。
      lowLatencyMode: false,
      // 切片是 1 秒一片, 所以这里的数字就是缓冲的秒数。
      // 6 而不是 4: 生成侧每句之间有约 4 秒的空档 (TTS 预热那段不出帧), 缓冲少于
      // 它就会反复见底 —— 而见底的表现正是卡顿和黑屏。用 2 秒延迟换不卡。
      liveSyncDurationCount: 6,
      liveMaxLatencyDurationCount: 20,   // 落后 20 秒才算真掉队
      // 关键的一条: 落后了**加速追**(最多 1.1 倍), 而不是跳过去。
      // 跳 = 缓冲被清 = 黑一下; 加速 10% 听感上几乎察觉不到。
      maxLiveSyncPlaybackRate: 1.1,
    });
    hls.loadSource(url);
    hls.attachMedia(v);
    hls.on(window.Hls.Events.MANIFEST_PARSED, function () { tryPlay(); });
    hls.on(window.Hls.Events.ERROR, function (_e, d) {
      if (!d.fatal) return;
      if (d.type === window.Hls.ErrorTypes.NETWORK_ERROR) { hls.startLoad(); return; }
      if (d.type === window.Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      retry += 1; playingUrl = '';
      note(t('reconnect'));
      setTimeout(refresh, Math.min(30000, 2000 * retry));
    });
  }

  /* 主播停了就把播放器拆掉。
     不拆的话下一次开播会**静静地什么都不发生**: 地址没变 (永远是同一个
     index.m3u8), play() 第一行的 `url === playingUrl` 守卫直接返回, 而那个 hls
     实例还卡在停播时的致命错误里。表现就是"停播之后再开播半天没反应" —— 页面
     上一切正常, 就是黑着。 */
  function teardown() {
    playingUrl = '';
    if (hls) { hls.destroy(); hls = null; }
    if (v) { try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) { /* 忽略 */ } }
  }

  function refresh() {
    return fetch('/api/live/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        online(!!d.live);
        if (!d.enabled) { teardown(); note(t('disabled')); return d; }
        if (!d.live) { teardown(); note(t('notlive')); return d; }
        note(''); retry = 0; play(d.hls);
        return d;
      })
      .catch(function () { note(t('reconnect')); return null; });
  }

  if (v) {
    v.addEventListener('playing', function () {
      note('');
      if (unmute) { unmute.hidden = false; paintSound(); }   // 起播了才给声音开关
    });
  }
  /* 声音是**开关**, 不是一次性的。原先点完就 hidden=true, 于是开了再也关不掉
     —— 而直播是会一直开着的, 想静音只能关掉整个页面。 */
  function paintSound() {
    if (!unmute) return;
    unmute.textContent = t(v.muted ? 'unmute' : 'mute');
    unmute.setAttribute('aria-pressed', v.muted ? 'false' : 'true');
  }
  if (unmute) {
    unmute.addEventListener('click', function () {
      v.muted = !v.muted;
      if (!v.muted) v.volume = 1;
      paintSound();
      tryPlay();
    });
    v.addEventListener('volumechange', paintSound);   // 系统/键盘改的也跟上
  }

  /* 卡住了自己爬起来。
   *
   * 产出慢于播放时缓冲会被慢慢抽干, 见底之后播放器就停在那儿 —— 观众看到的是
   * 画面定住, 而服务端一切正常 (流照跑、状态照报直播中)。以前只能靠刷新页面。
   *
   * 判据是 **currentTime 连着几拍没往前走**, 不是 waiting/stalled 事件: 那两个
   * 事件在缓冲见底时不一定发 (这一页头注释里为别的原因记过同一件事), 而漏一次
   * 的代价是画面永远停在那儿。
   */
  var STUCK_TICKS = 6;            // 每秒一拍; 6 秒不动才算卡, 短了会误伤正常抖动
  var lastT = -1, stuckFor = 0;
  setInterval(function () {
    if (!v || v.paused || !playingUrl) { stuckFor = 0; lastT = -1; return; }
    if (v.currentTime !== lastT) { lastT = v.currentTime; stuckFor = 0; return; }
    if (++stuckFor < STUCK_TICKS) return;
    stuckFor = 0;
    // 跳到直播边缘 —— 卡住期间落下的那几十秒没有追的价值, 观众要看的是"现在"。
    try {
      if (hls) {
        hls.startLoad();
        var p = hls.liveSyncPosition;
        if (p && isFinite(p)) v.currentTime = p;
      } else if (v.seekable && v.seekable.length) {
        // Safari 原生放 HLS 时 hls 恒为 null —— 以前这条路**完全没有恢复手段**,
        // 而创始人用的就是 Mac。
        v.currentTime = Math.max(0, v.seekable.end(v.seekable.length - 1) - 1);
      }
      tryPlay();
    } catch (e) { /* 跳失败就等下一拍再来 */ }
  }, 1000);

  refresh();
  setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!hls || (v && v.paused))) refresh();
  });

  return { refresh: refresh, online: online };
})();
