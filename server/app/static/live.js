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

  function play(url) {
    if (!v) return;
    if (url === playingUrl && hls && !v.paused) return;
    playingUrl = url;
    if (hls) { hls.destroy(); hls = null; }
    if (v.canPlayType('application/vnd.apple.mpegurl')) {   // Safari 原生放 HLS
      v.src = url; v.play().catch(function () {}); return;
    }
    if (!window.Hls || !window.Hls.isSupported()) { note(t('unsupported')); return; }
    hls = new window.Hls({ lowLatencyMode: true, liveSyncDurationCount: 3 });
    hls.loadSource(url);
    hls.attachMedia(v);
    hls.on(window.Hls.Events.MANIFEST_PARSED, function () { v.play().catch(function () {}); });
    hls.on(window.Hls.Events.ERROR, function (_e, d) {
      if (!d.fatal) return;
      if (d.type === window.Hls.ErrorTypes.NETWORK_ERROR) { hls.startLoad(); return; }
      if (d.type === window.Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      retry += 1; playingUrl = '';
      note(t('reconnect'));
      setTimeout(refresh, Math.min(30000, 2000 * retry));
    });
  }

  function refresh() {
    return fetch('/api/live/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        online(!!d.live);
        if (!d.enabled) { note(t('disabled')); return d; }
        if (!d.live) { note(t('notlive')); return d; }
        note(''); retry = 0; play(d.hls);
        return d;
      })
      .catch(function () { note(t('reconnect')); return null; });
  }

  if (v) {
    v.addEventListener('playing', function () {
      note('');
      if (v.muted && unmute) unmute.hidden = false;   // 起播了才提示开声音
    });
  }
  if (unmute) {
    unmute.addEventListener('click', function () {
      v.muted = false; v.volume = 1; unmute.hidden = true; v.play().catch(function () {});
    });
  }

  refresh();
  setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!hls || (v && v.paused))) refresh();
  });

  return { refresh: refresh, online: online };
})();
