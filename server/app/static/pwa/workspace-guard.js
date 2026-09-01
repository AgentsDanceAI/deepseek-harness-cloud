// 工作台守护层 —— 所有产品共用, 由服务端注入进工作台文档 (见 _guard_inject)。
//
// 干两件事:
//
// ① **心跳**。回收判据原先靠"请求流量"当在场信号 (Caddy 的 forward_auth 会为
//    页面上每个资源打一次 /api/work/route)。但流量治不了**长连接**: 一条挂着
//    几分钟的 SSE 期间, 页面一个新请求都不发 —— 看起来就跟标签页关了一模一样。
//    2026-09-01 老板守着浏览器看剧组跑片, 容器被判 "tab closed" 收掉, 屏幕上
//    只留一句 "Load failed"。心跳是主动的, 不受连接形态影响。
//
// ② **回收前弹窗**。判据总有失灵的时候 (那次就是 agent 活动信号传错了键,
//    一直读到 0), 而代价是用户正干着的活当场没了。所以服务端改成"先挂牌再等",
//    这里把牌子变成一个看得见的确认框: 120 秒倒计时, 点「继续用」就摘牌。
//
// 页面不可见时不发心跳 —— 那时本来就该按空闲计。切回来立刻补一发, 否则用户
// 回到标签页的头几秒里仍可能被收掉。
(function () {
  'use strict';
  if (window.__dshGuard) return;          // 注入两次也只跑一份
  window.__dshGuard = true;

  var HEARTBEAT_MS = 45000;               // 远小于最短宽限 (3 分钟), 留足余量
  var POLL_MS = 15000;                    // 挂牌到弹窗之间最多晚 15 秒
  var box = null, timer = null, left = 0;

  function post(path) {
    return fetch(path, { method: 'POST', credentials: 'same-origin' }).catch(function () {});
  }

  function beat() {
    if (document.visibilityState === 'hidden') return;
    post('/api/work/heartbeat');
  }

  function close() {
    if (timer) { clearInterval(timer); timer = null; }
    if (box && box.parentNode) box.parentNode.removeChild(box);
    box = null;
  }

  function keep() {
    close();
    post('/api/work/keep').then(beat);
  }

  // 弹窗自带全部样式并挂在 <body> 末尾 —— 宿主是 Dify/Coze/ComfyUI 这些第三方
  // 界面, 不能假设它们有任何 CSS 框架, 也不能污染它们的类名。
  function open(seconds) {
    if (box) return;
    left = seconds;
    box = document.createElement('div');
    box.setAttribute('style', [
      'position:fixed', 'inset:0', 'z-index:2147483647',
      'display:flex', 'align-items:center', 'justify-content:center',
      'background:rgba(0,0,0,.45)',
      'font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif',
    ].join(';'));
    box.innerHTML =
      '<div style="max-width:380px;margin:16px;padding:22px 24px;border-radius:14px;' +
      'background:#fff;color:#1a1a1a;box-shadow:0 18px 48px rgba(0,0,0,.28)">' +
      '<div style="font-size:16px;font-weight:600;margin-bottom:8px">这台云电脑还在用吗？</div>' +
      '<div style="color:#555;margin-bottom:6px">看起来闲了一会儿。' +
      '<b id="dsh-guard-n"></b> 秒后会自动回收以省下机时。</div>' +
      '<div style="color:#888;font-size:12.5px;margin-bottom:18px">' +
      '回收只是关机 —— 文件都在, 下次打开还能接着用。</div>' +
      '<div style="display:flex;gap:10px;justify-content:flex-end">' +
      '<button id="dsh-guard-stop" style="padding:8px 14px;border-radius:9px;border:1px solid #ddd;' +
      'background:#fff;color:#555;cursor:pointer">现在就收</button>' +
      '<button id="dsh-guard-keep" style="padding:8px 16px;border-radius:9px;border:0;' +
      'background:#4c6ef5;color:#fff;font-weight:600;cursor:pointer">继续用</button>' +
      '</div></div>';
    document.body.appendChild(box);
    var n = box.querySelector('#dsh-guard-n');
    n.textContent = left;
    box.querySelector('#dsh-guard-keep').onclick = keep;
    // 「现在就收」不主动调停止端点 —— 让它自然走完倒计时即可, 少一条能误伤的路径
    box.querySelector('#dsh-guard-stop').onclick = close;
    timer = setInterval(function () {
      left -= 1;
      if (left <= 0) { close(); return; }
      n.textContent = left;
    }, 1000);
  }

  function poll() {
    fetch('/api/work/reclaim', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (s && s.asking) open(s.seconds_left || 0);
        else close();                     // 服务端摘牌了 (比如别处有了活动)
      })
      .catch(function () {});
  }

  beat();
  poll();
  setInterval(beat, HEARTBEAT_MS);
  setInterval(poll, POLL_MS);
  // 切回标签页立刻补一发 —— 否则回来的头几秒里仍可能被收掉
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') { beat(); poll(); }
  });
})();
