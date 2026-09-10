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
    if (!pr || !pr.catch) return;
    pr.catch(function () {
      /* 非静音被拒 —— **先退回静音播放, 别把整块画面变成"点一下"的黑屏**。
       *
       * 什么时候会走到这: 管理员在控制台按一次保存, live_server 就把播出停掉重开
       * (那是话术即时生效的代价, 见它的 put_room)。观众这边 teardown 之后重连,
       * 而此时元素已经被观众解除静音过 —— 浏览器不放行非静音自动播放, play() 被拒。
       * 旧写法直接挂出"点一下开始播放": 观众看到的是**画面没了**, 只会以为直播挂了。
       * 退回静音至少画面是连着的, 声音一键就能拿回来。 */
      if (!v.muted) {
        v.muted = true;
        paintSound();
        note(t('remuted'));
        var again = v.play();
        if (again && again.catch) {
          again.catch(function () { note(t('tapplay')); armTap(); });
        }
        return;
      }
      note(t('tapplay'));
      armTap();
    });
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
      // Safari 自己挑直播位置, 通常只留三个目标时长 —— 1 秒一片时就是 3 秒跑道,
      // 而生成侧是 0.9×, 三秒钟就见底。hls.js 那边靠 liveSyncDuration 拉开,
      // 原生这条没有那个旋钮, 只能起播后自己往回退。
      v.src = url;
      v.addEventListener('loadedmetadata', function once() {
        v.removeEventListener('loadedmetadata', once);
        seekBehindLive();
      });
      tryPlay(); return;
    }
    if (!window.Hls || !window.Hls.isSupported()) { note(t('unsupported')); return; }
    hls = new window.Hls({
      // 我们发的是**普通** HLS (2 秒整片, 没有 EXT-X-PART)。开 lowLatencyMode 只会
      // 让它按低延迟那套去贴直播边缘, 落后一点就纠正 —— 而纠正的方式是**跳**。
      lowLatencyMode: false,
      // 切片是 1 秒一片, 所以这里的数字就是缓冲的秒数。
      //
      // 6 -> 12 (2026-09-09): 生成侧实测只有 **0.9× 实时** —— 低于 1.0 时缓冲必然
      // 被慢慢抽干, 缓冲多大只决定"多久见底": 6 秒缓冲约 1 分钟见底, 12 秒约 2 分钟
      // (见底速率 = 1 - 0.9 = 0.1×)。创始人给领导演示时就是看了一分多钟卡住的。
      // 代价是观众晚 6 秒看到 —— 这一页本来就不是互动视频 (回一条评论要十几秒),
      // 6 秒换一倍的续航是划算的。
      // ⚠️ 2026-09-10: 上面这段"数字就是缓冲的秒数"是**错的**, 别照着推。count 要
      // 乘 TARGETDURATION, 而我们的 TARGETDURATION 是 **2** (片长约 0.96 秒, 但
      // HLS 规范要求向上取整)。所以 `12` 实际要的是 24 秒, 而播放列表窗一共才
      // 15.3 秒 —— 要不到, hls.js 只能把观众钉在窗口**最老那一片**上: 对"产慢了"
      // 最耐受, 对"产快了"零容忍。改用**秒**为单位, 这类乘法坑就不存在了。
      //
      // 而产出侧换成百炼 TTS 之后实测 **1.65× 实时**, 问题正好翻了个面: 直播边缘
      // 每秒推进 1.85 片, 观众每秒往后掉 0.85 秒, 十几秒就被甩出窗口, 播放器只能
      // 往前跳。治本在产出侧(live_server.py 按墙钟把产出压回 1.0×), 这里配合它定位。
      //
      // ⚠️ 这个数**必须大于产出侧的空档**。节流让产出变成锯齿: 句内每 0.5~1.1 秒
      // 出片, 句间有 5~8 秒空档(实测 75 秒内七次, 最大 8.0 秒) —— 那是压回 1.0× 的
      // 固有代价。落后不够多, 空档一来缓冲就见底: **画面停、没声音, 过几秒又恢复**。
      // 7 -> 12 (2026-09-10): 7 秒小于 8 秒的空档, 线上真出了这个症状。
      // 12 秒配 38 秒的窗(live_server.py LIST_SIZE=40): 前面挡得住 8 秒空档,
      // 后面离窗沿还有二十多秒, 两头都不紧张。
      liveSyncDuration: 12,
      liveMaxLatencyDuration: 25,   // 25 < 窗长 38, 这条才真会触发
      // 1.1 -> 1 (2026-09-10): **关掉变速追赶。**
      // 这行是按"产出不足、观众持续落后、要一路追"的年代调的。现在产出被墙钟节流
      // 压在 1.0×, 观众不会持续落后; 但节流让产出变成锯齿(句内出片、句间空 5~8 秒),
      // 于是变成每十秒一个来回: 爆发后落后变大 -> 提速 1.1×, 空档里落回 -> 降回 1.0×。
      // **语速每十秒变一次, 听感就是一顿一顿的**(创始人 2026-09-10 报"一卡一卡")。
      // 窗长 38 秒、落后 12 秒, 余量足够, 不需要追; 真掉队了还有
      // liveMaxLatencyDuration(25 秒) 兜底。
      maxLiveSyncPlaybackRate: 1,
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
  //: 恢复时退到直播边缘**之后**这么多秒。贴着边缘恢复的话, 0.9× 的产出几秒钟就
  //: 又把它抽干, 于是变成每隔几秒跳一次 —— 比一直卡着还难看。
  // 落后直播边缘多少秒。必须大于产出侧的句间空档(实测最大 8 秒), 否则缓冲见底,
  // 表现为"画面停、没声音"。与 hls.js 的 liveSyncDuration 保持同一个数。
  var BEHIND_LIVE = 12;

  function seekBehindLive() {
    if (!v || !v.seekable || !v.seekable.length) return;
    var edge = v.seekable.end(v.seekable.length - 1);
    if (!isFinite(edge)) return;
    var want = Math.max(0, edge - BEHIND_LIVE);
    if (want > v.currentTime) v.currentTime = want;
  }
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
      } else {
        // Safari 原生放 HLS 时 hls 恒为 null —— 以前这条路**完全没有恢复手段**,
        // 而创始人用的就是 Mac。
        seekBehindLive();
      }
      tryPlay();
    } catch (e) { /* 跳失败就等下一拍再来 */ }
  }, 1000);

  refresh();
  setInterval(refresh, 15000);
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!hls || (v && v.paused))) refresh();
  });

  /* 落后直播边缘多少秒。**字幕靠它对齐**, 所以要挑最准的来源。
   *
   * ⚠️ 别用 video.seekable: hls.js 走 MSE, seekable 常常只反映**已缓冲范围**,
   * 播放器明明落后十几秒, 它也能算出接近 0 —— 拿它对字幕等于没对
   * (2026-09-10 创始人第三次报"字幕对不齐", 根子就在这)。
   * hls.js 自己算的 latency 才是距直播边缘的真实延迟; 退一步用 liveSyncPosition。 */
  function lag() {
    if (!v) return 0;
    var l = null;
    if (hls) {
      if (typeof hls.latency === 'number' && isFinite(hls.latency) && hls.latency > 0) {
        l = hls.latency;
      } else if (typeof hls.liveSyncPosition === 'number' && isFinite(hls.liveSyncPosition)) {
        l = hls.liveSyncPosition - v.currentTime;
      }
    }
    if (l === null && v.seekable && v.seekable.length) {   // Safari 原生 HLS 只有这条
      var edge = v.seekable.end(v.seekable.length - 1);
      if (isFinite(edge)) l = edge - v.currentTime;
    }
    if (l === null || !isFinite(l) || !(l > 0)) return 0;
    return l > 120 ? 120 : l;      // 离谱值当没有, 别把字幕甩到几分钟前
  }

  return { refresh: refresh, online: online, lag: lag };
})();
