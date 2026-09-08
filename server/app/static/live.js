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
    // ⚠️ 地址没变就**什么都别做**。这里原先还判了 `!v.paused` —— 于是只要视频那
    // 一刻是暂停的 (切到后台、刚起播、浏览器自己按下的暂停), 每 15 秒一次的状态
    // 刷新就会走下去把整个 hls 实例销毁重建。MediaSource 一拆一建, 画面就黑一下,
    // 而日志里什么都不会有。("断的时候会黑一下" —— 老板 2026-09-08)
    // 暂停了该做的是让它继续播, 不是重建播放器。
    if (url === playingUrl && hls) {
      if (v.paused) v.play().catch(function () {});
      return;
    }
    playingUrl = url;
    if (hls) { hls.destroy(); hls = null; }
    if (v.canPlayType('application/vnd.apple.mpegurl')) {   // Safari 原生放 HLS
      v.src = url; v.play().catch(function () {}); return;
    }
    if (!window.Hls || !window.Hls.isSupported()) { note(t('unsupported')); return; }
    hls = new window.Hls({
      // 我们发的是**普通** HLS (2 秒整片, 没有 EXT-X-PART)。开 lowLatencyMode 只会
      // 让它按低延迟那套去贴直播边缘, 落后一点就纠正 —— 而纠正的方式是**跳**。
      lowLatencyMode: false,
      liveSyncDurationCount: 4,          // 8 秒缓冲, 够扛一次网络抖动
      liveMaxLatencyDurationCount: 12,   // 落后 24 秒才算真掉队
      // 关键的一条: 落后了**加速追**(最多 1.1 倍), 而不是跳过去。
      // 跳 = 缓冲被清 = 黑一下; 加速 10% 听感上几乎察觉不到。
      maxLiveSyncPlaybackRate: 1.1,
    });
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
