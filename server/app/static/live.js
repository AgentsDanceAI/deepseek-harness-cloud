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
  //: 正在播的是哪一场 (开播时刻)。地址永远是同一个 index.m3u8, 所以只有它
  //: 能告诉我们"换了一场" —— 换场时播放列表被重建, 手里的缓冲全作废。
  var playingSince = 0;

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
  /* 把播放器遇到的麻烦报给服务端。
   *
   * 为什么要有它: 播放器**早就知道**自己什么时候冻住、什么时候在等数据 —— 卡死
   * 检测每秒一拍, 冻住 6 秒就自己跳一下。但它从来不说, 于是"观众到底卡了几次"这个
   * 问题一直没人答得上来, 每次报障都只能现场架探针去量, 回头什么都查不到
   * (创始人 2026-09-10 问到)。
   *
   * ⚠️ 失败**完全忽略**: 观测坏了不该在播放页上冒红字, 更不该重试 —— 卡顿的时候
   *    网络本来就不好, 重试只会雪上加霜。
   * ⚠️ 同一种事件本地先压一道(服务端还有一道), 卡住时事件是连着来的。 */
  var lastReport = {};
  /* 这份 live.js 是哪一版。**跟事件一起报上来** —— 否则"用户刷没刷新"只能靠猜,
     而新旧代码的表现完全不同(2026-09-10: 我按新参数分析了半天, 实际他跑的是旧的,
     指纹是 waiting 的落后恒等于旧的 liveSyncDuration)。
     取的是加载这个脚本时用的 ?v= —— 服务端拿它做缓存击穿, 正好就是版本号。 */
  function clientVer() {
    try {
      var el = document.currentScript;
      if (!el) {
        var all = document.getElementsByTagName('script');
        for (var i = 0; i < all.length; i++) {
          if (/live\.js/.test(all[i].src || '')) { el = all[i]; break; }
        }
      }
      var m = /[?&]v=([0-9]+)/.exec((el && el.src) || '');
      return m ? m[1] : '?';
    } catch (e) { return '?'; }
  }
  var VER = clientVer();

  /* 出事那一刻播放器手里到底有什么。**这三个数能直接定死是哪一类问题**, 不用猜:
   *   buf  = 播放头前面还缓冲着多少秒。0 = 断粮; 大 = 有数据却播不动(空洞/解码)。
   *   bw   = hls.js 估的下行带宽。这条流约 2.1 Mbps, 低于它就是送不过来。
   *   rs   = readyState。0/1 = 浏览器认为自己没数据; 4 = 有数据却不动。
   * 2026-09-10: 我拿服务端指标猜了一晚上观众为什么卡, 每次都猜错。产出健康、
   * 时间轴连续、我这边带宽 5.2Mbps —— 但这些都不是观众那条路径上的量。 */
  function snapshot() {
    var out = [];
    try {
      var ahead = 0;
      if (v && v.buffered) {
        for (var i = 0; i < v.buffered.length; i++) {
          if (v.buffered.start(i) <= v.currentTime + 0.1
              && v.buffered.end(i) > v.currentTime) {
            ahead = v.buffered.end(i) - v.currentTime;
          }
        }
      }
      out.push('缓冲' + ahead.toFixed(1) + 's');
      if (hls && isFinite(hls.bandwidthEstimate)) {
        out.push('带宽' + (hls.bandwidthEstimate / 1e6).toFixed(1) + 'Mbps');
      }
      if (v) out.push('rs' + v.readyState);
    } catch (e) { /* 观测坏了不该影响播放 */ }
    return out.join(' ');
  }

  function reportIssue(kind, secs, detail) {
    try {
      var now = Date.now();
      if (lastReport[kind] && now - lastReport[kind] < 20000) return;
      lastReport[kind] = now;
      fetch('/api/live/report', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: kind,
          secs: secs || 0,
          lag: lag(),                       // 出事时观众落后直播边缘多少
          detail: (String(detail || '') + ' | ' + snapshot()
                   + ' [v' + VER + ']').slice(0, 200),
        }),
      }).catch(function () { /* 观测坏了不该影响播放 */ });
    } catch (e) { /* 同上 */ }
  }

  var tapArmed = false;
  /* fromTap: 这次是**人点出来的**。
     区分它是因为 play() 被拒有两种完全不同的原因, 而以前一律当成前者:
       · 没点过就播 —— 浏览器的自动播放策略, 挂个"点一下"就解决了;
       · **点了还被拒** —— 不是策略问题, 是播放器废了(MSE 缓冲坏掉、切片全 404),
         再挂"点一下"是死循环。创始人 2026-09-10 撞到: 18:10~18:16 连着七次
         "自动播放被拒", 每次点完又弹一次(见 live_incidents 表)。 */
  function tryPlay(fromTap) {
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
        reportIssue('remuted', 0, '非静音被拒, 退回静音');
        var again = v.play();
        if (again && again.catch) {
          again.catch(function () { giveUp(fromTap); });
        }
        return;
      }
      giveUp(fromTap);
    });
  }

  /* 静音也播不了的时候怎么办。 */
  function giveUp(fromTap) {
    if (fromTap) {
      // 点了还播不了 = 播放器废了, 再挂"点一下"没有意义。拆掉重建 —— 下一次
      // refresh() 会重新起一个干净的实例。
      reportIssue('fatal', 0, '点击后仍无法播放, 重建播放器');
      note(t('reconnect'));
      teardown();
      refresh();
      return;
    }
    note(t('tapplay'));
    reportIssue('autoplay', 0, '自动播放被拒, 挂出「点一下」');
    armTap();
  }
  function armTap() {
    var stage = v && v.parentNode;
    if (!stage || tapArmed) return;
    tapArmed = true;
    stage.addEventListener('click', function once() {
      stage.removeEventListener('click', once);
      tapArmed = false;
      note('');
      tryPlay(true);          // 人点的 —— 再失败就是播放器废了, 不是策略问题
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
      // 12 -> 18 (2026-09-10 晚): 实测句间空档最大 **9.2 秒**, 12 秒的缓冲只剩
      // 2.8 秒余量 —— 一个稍大的空档就见底。18 秒留 8.8 秒余量, 而窗长 38.6 秒,
      // 后面还剩二十秒, 两头都不紧张。代价是回评论晚 6 秒看到。
      // 18 -> 5 (2026-09-10 晚, 播出时钟上线之后)。
      // 上面那一长串 7→12→18 的加码, 每一次都是在**追产出侧的锯齿**: 播放列表以前是
      // "一句转完冒 9 秒、再空 5~9 秒"的形状, 缓冲必须大于最大空档, 于是只能一路加。
      // live_server 现在按墙钟一秒发一片(见它的 _publisher), 播放列表匀速前进,
      // 实测空档中位数 1 秒上下 —— 缓冲就不必再买保险了。
      // 这个数直接就是"回一条评论要等多久"里最大的一项, 降 13 秒等于观众早 13 秒听见。
      // ⚠️ 再往下调之前先量 gap_p90: 缓冲要大于播放列表的空档 p90, 这是硬约束。
      // 5 -> 6: 切片改回 2 秒一片(见 live_server 里 HLS_SEC 那段实测), 缓冲要装得下
      // 三片才不至于一片没到就见底。
      liveSyncDuration: 6,
      liveMaxLatencyDuration: 24,   // 24 < 窗长 40(LIST_SIZE=20 × 2 秒), 这条才真会触发
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
      reportIssue('fatal', 0, String((d && d.details) || 'unknown'));
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
    playingSince = 0;
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
        // **换了一场就必须拆掉重来。** 切形象/音色是在同一个请求里 stop+start,
        // 这里很可能从头到尾都看到 live:true —— 但播放列表已经被 rmtree 重建,
        // MEDIA-SEQUENCE 退回 0, 手里缓冲的切片全 404, 时间轴还倒退了。
        // 地址永远是同一个 index.m3u8, play() 开头的 `url === playingUrl` 守卫
        // 会直接返回, 于是播放器一直卡在废掉的 MSE 缓冲上: 画面全黑, 点一下也
        // 没反应(创始人 2026-09-10 切形象后撞到)。
        if (d.since && playingSince && d.since !== playingSince) {
          reportIssue('rebuild', 0, '换场重建');
          teardown();
        }
        note(''); retry = 0; play(d.hls);
        playingSince = d.since || playingSince;
        return d;
      })
      .catch(function () { note(t('reconnect')); return null; });
  }

  /* 缓冲见底: `waiting` 是浏览器**拿不到下一帧**时发的, 这就是"播一会儿没声音,
     过几秒又有"的精确信号 —— 比卡死检测(要冻住 6 秒才算)灵敏得多, 短暂的一顿也能
     抓到。配对的 `playing` 给出它到底停了多久。
     ⚠️ 0.4 秒以下不报: 正常起播和拖进度条都会发 waiting, 全报就是噪声。 */
  var waitAt = 0;
  if (v) {
    v.addEventListener('waiting', function () {
      if (!v.paused && playingUrl) waitAt = Date.now();
    });
    v.addEventListener('playing', function () {
      note('');
      if (unmute) { unmute.hidden = false; paintSound(); }   // 起播了才给声音开关
      if (waitAt) {
        var held = (Date.now() - waitAt) / 1000;
        waitAt = 0;
        if (held >= 0.4) reportIssue('waiting', held, '缓冲见底 ' + held.toFixed(1) + ' 秒');
      }
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
  // 落后直播边缘多少秒。与 hls.js 的 liveSyncDuration 必须是同一个数 —— Safari 走
  // 原生 HLS 时 hls 恒为 null, 上面那些参数它一条都吃不到, 只有这个常数管着它。
  // 18 -> 6: 播出时钟上线后播放列表匀速前进, 不再需要为句间空档买保险;
  // 6 = 三片 × 2 秒, 与上面的 liveSyncDuration 同一个数。
  var BEHIND_LIVE = 6;
  /* 落后同步点多远才值得**跳**过去。
     跳是有代价的: 它把已经缓冲的内容全丢掉, 而且本身就是一次可见的跳段。所以只在
     "再不跳就永远动不了"时才跳 —— 也就是播放位置快掉出窗口后沿了。
     ⛔ 以前是只要卡住 6 秒就无条件跳, 结果是个自我维持的循环(2026-09-10 实测):
        空档耗干缓冲 -> 冻 6 秒 -> 跳 -> **跳把缓冲清空** -> 下一个空档立刻又见底
        -> 再冻 6 秒。观众感受到的就是"一卡一卡"。 */
  var SEEK_IF_BEHIND = 12;
  /* 卡在缓冲空洞上时往前推多少。
     指纹: **画面冻住(stall)但浏览器从没报过没数据(waiting)** —— 缓冲不空却播不动,
     那是 MSE 的播放位置正好落在一个空洞里(每句独立编码再按累计偏移拼接, 句边界上
     可能差几毫秒)。这种卡只需要推过去, 不需要重新定位。
     ⛔ 别用"跳回同步点"来解决它: 那会把整个缓冲丢掉, 下一个产出空档立刻又见底,
        于是循环(2026-09-10 实测)。也别不管: 上一版改成"掉太远才跳"之后, 落后没超
        门槛的就永远冻着 —— 创始人报"半天还没有说话"。 */
  /* ⛔ 别用固定步长推。洞有多大是不知道的, 而卡死检测 6 秒才响一次 —— 一次推
     0.3 秒等于要卡好几分钟才跨得过去(2026-09-10 实测就是这样)。
     正确做法: **跨到下一段缓冲的起点**。指纹是上报里的 `缓冲8.0s rs2` —— 播放头
     前面明明有 8 秒数据却接不上下一帧, 说明它正落在洞里, 洞后面就是那 8 秒。 */
  function nextBufferedStart() {
    if (!v || !v.buffered) return 0;
    for (var i = 0; i < v.buffered.length; i++) {
      var a = v.buffered.start(i);
      if (a > v.currentTime + 0.01) return a;
    }
    return 0;
  }

  /* 起播时往回退, 把跑道拉开。**只用于起播**, 不能用来救卡住的播放器。 */
  function seekBehindLive() {
    if (!v || !v.seekable || !v.seekable.length) return;
    var edge = v.seekable.end(v.seekable.length - 1);
    if (!isFinite(edge)) return;
    var want = Math.max(0, edge - BEHIND_LIVE);
    if (want > v.currentTime) v.currentTime = want;
  }

  /* 卡住时往**前**挪。
     ⛔ 以前 Safari 那条路卡住了是调 seekBehindLive(), 而它只在
        `edge - BEHIND_LIVE > currentTime` 时才动 —— Safari 原生播放自己就贴在边缘后
        约 6 秒(它只留三个目标时长的跑道), 而 BEHIND_LIVE 是 18, 条件恒为假,
        **等于什么都没做**。于是 Safari 卡住之后没有任何脱困手段, 永远冻着。
        而 BEHIND_LIVE 从 10 一路调到 18, 只让这个条件更不可能成立 —— 是我把它调坏的。
        创始人 2026-09-10: "safari 卡住不动, chrome 正常"。 */
  function seekForwardLive() {
    if (!v || !v.seekable || !v.seekable.length) return;
    var edge = v.seekable.end(v.seekable.length - 1);
    if (!isFinite(edge) || edge <= v.currentTime + 0.2) return;
    var want = Math.max(v.currentTime + 1, edge - BEHIND_LIVE);
    v.currentTime = Math.min(want, edge - 0.5);
  }
  var lastT = -1, stuckFor = 0;
  setInterval(function () {
    if (!v || v.paused || !playingUrl) { stuckFor = 0; lastT = -1; return; }
    if (v.currentTime !== lastT) { lastT = v.currentTime; stuckFor = 0; return; }
    if (++stuckFor < STUCK_TICKS) return;
    // ⚠️ 报的秒数是**阈值**不是真实冻结时长(卡死检测到 6 秒就动手了)。真实时长
    //    要看落后量涨了多少。以前写 6.0 会让人以为每次都正好冻 6 秒。
    reportIssue('stall', stuckFor, (hls ? '' : 'Safari ') + '画面冻住(阈值6s)');
    stuckFor = 0;
    // 跳到直播边缘 —— 卡住期间落下的那几十秒没有追的价值, 观众要看的是"现在"。
    try {
      if (hls) {
        // 先把拉流催起来 —— 大多数时候这就够了, 前面只是暂时没数据。
        hls.startLoad();
        var p = hls.liveSyncPosition;
        if (p && isFinite(p) && p - v.currentTime > SEEK_IF_BEHIND) {
          // 掉得太远(快掉出窗口后沿) —— 只能跳回同步点, 代价是丢掉缓冲。
          v.currentTime = p;
        } else {
          // 还在窗口里: 卡的是缓冲空洞。跨到下一段缓冲的起点 —— 洞多大都能跨,
          // 而且**保住后面那段缓冲**(跳回同步点会把它一起丢掉)。
          var nb = nextBufferedStart();
          if (nb) v.currentTime = nb + 0.05;
          else if (p && isFinite(p)) v.currentTime = p;   // 后面没数据: 只能回同步点
        }
      } else {
        // Safari 原生放 HLS 时 hls 恒为 null。
        // v.buffered 两个浏览器都有, 所以跨洞这条对 Safari 一样适用 —— 优先用它,
        // 它能保住洞后面那段缓冲。跨不了(后面确实没数据)才往前贴近边缘。
        var nb2 = nextBufferedStart();
        if (nb2) v.currentTime = nb2 + 0.05;
        else seekForwardLive();
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
