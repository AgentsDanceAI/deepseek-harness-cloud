/* 数字人直播间播放器。
 *
 * 只做三件事: 问状态 → 起播 → 断了自己接回来。
 *
 * 两个必须讲清楚的坑:
 *  1. 自动播放策略: 只有**静音**才允许自动起播。所以 video 标签上就带 muted,
 *     出声靠一个按钮 (那一下有用户手势)。不这么做的表现是整页安静地什么都不
 *     发生, 只有控制台里一行 NotAllowedError。
 *  2. 直播流会断 (切片轮转、网络抖动、上游重启)。hls.js 的致命错误必须自己接,
 *     否则播放器就永远停在最后一帧, 而**看上去和"主播不说话"一模一样**。
 */
(function () {
  var v = document.getElementById('lvVideo');
  var msg = document.getElementById('lvMsg');
  var badge = document.getElementById('lvBadge');
  var unmute = document.getElementById('lvUnmute');
  var hls = null, retry = 0;

  function say(t) { msg.textContent = t || ''; msg.hidden = !t; }
  function online(on) {
    badge.textContent = badge.dataset[on ? 'on' : 'off'];
    badge.className = 'lv-badge lv-badge--' + (on ? 'on' : 'off');
  }

  function start(url) {
    if (hls) { hls.destroy(); hls = null; }
    if (v.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari 原生放 HLS —— 不必也不该再套一层 hls.js。
      v.src = url;
      v.play().catch(function () {});
      return;
    }
    if (!window.Hls || !window.Hls.isSupported()) { say(badge.dataset.unsupported); return; }
    hls = new window.Hls({ lowLatencyMode: true, liveSyncDurationCount: 3 });
    hls.loadSource(url);
    hls.attachMedia(v);
    hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
      v.play().catch(function () {});
    });
    hls.on(window.Hls.Events.ERROR, function (_e, data) {
      if (!data.fatal) return;
      // 网络类错误重试加载, 媒体类错误让它自己恢复; 都不行就整个重建。
      if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) { hls.startLoad(); return; }
      if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      retry += 1;
      say(badge.dataset.reconnect);
      setTimeout(boot, Math.min(30000, 2000 * retry));
    });
  }

  function boot() {
    fetch('/api/live/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        online(!!d.live);
        if (!d.enabled) { say(badge.dataset.disabled); return; }
        if (!d.live) { say(badge.dataset.notlive); setTimeout(boot, 15000); return; }
        say('');
        retry = 0;
        start(d.hls);
      })
      .catch(function () { say(badge.dataset.reconnect); setTimeout(boot, 8000); });
  }

  v.addEventListener('playing', function () {
    say('');
    // 起播了才提示开声音 —— 没画面就先别喊人点按钮。
    if (v.muted) unmute.hidden = false;
  });
  unmute.addEventListener('click', function () {
    v.muted = false;
    v.volume = 1;
    unmute.hidden = true;
    v.play().catch(function () {});
  });

  boot();
  // 页面切回前台时对一次状态: 后台标签页的定时器会被节流到几分钟一次。
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!hls || v.paused)) boot();
  });
})();
