/* 直播间公屏: 飘屏 + 发言。观看页与控制台共用。
 *
 * 飘屏是**纯前端**的一层覆盖, 不碰 GPU —— 她开不开口是另一回事 (服务端按队列和
 * 冷却决定)。所以就算她答不过来, 公屏也照样热闹。
 *
 * 两个刻意的选择:
 *  1. 轮询而不是 WebSocket。这一页本来就每 15 秒问一次状态, 公屏跟着问一次而已;
 *     上 WS 要多一条长连接、多一套重连, 而公屏晚两秒到没人会死。
 *  2. 飘完就删。DOM 里只留在飞的那几条 —— 挂着几百个绝对定位的元素, 手机上会卡。
 */
window.LiveChat = (function () {

  /* 当前是哪一间。写在 badge 的 data-room 上 —— 这一页所有传给后端的字符串都
     从这里取, 不从 URL 现解: URL 是浏览器给的, 而这个是服务端渲染进来的。 */
  function room() {
    var b = document.getElementById('lvBadge');
    return (b && b.dataset.room) || '';
  }
  var stage = document.getElementById('lvVideo');
  stage = stage && stage.parentNode;
  var box = document.getElementById('lvDanmu');
  var input = document.getElementById('lvSayBox');
  var send = document.getElementById('lvSayBtn');
  var hint = document.getElementById('lvSayHint');
  var since = 0, seen = {}, lane = 0;

  function t(k) { return (hint && hint.dataset[k]) || ''; }

  /* 一条飘屏。轨道循环用, 相邻两条不会完全重叠。 */
  function fly(item) {
    if (!box) return;
    var el = document.createElement('div');
    el.className = 'lv-dm' + (item.replied ? ' lv-dm--replied' : '');
    el.style.top = (6 + (lane % 6) * 15) + '%';
    lane += 1;
    var who = document.createElement('b');
    who.textContent = (item.nick || '') + '：';
    el.appendChild(who);
    el.appendChild(document.createTextNode(item.text || ''));
    box.appendChild(el);
    // 动画结束就摘掉 —— 不摘的话开一小时会攒上千个节点。
    el.addEventListener('animationend', function () {
      if (el.parentNode) el.parentNode.removeChild(el);
    });
  }

  function pull() {
    return fetch('/api/live/comments?room=' + encodeURIComponent(room()) + '&since=' + encodeURIComponent(since), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.items) return;
        d.items.forEach(function (x) {
          if (seen[x.id]) return;
          seen[x.id] = 1;
          if (x.t > since) since = x.t;
          fly(x);
        });
      })
      .catch(function () {});
  }

  function post() {
    var text = input && input.value.trim();
    if (!text) return;
    send.disabled = true;
    fetch('/api/live/comment', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: room(), text: text }),
    }).then(function (r) {
      if (r.status === 401) { hint.textContent = t('needlogin'); return null; }
      if (r.status === 429) { hint.textContent = t('toofast'); return null; }
      if (!r.ok) { hint.textContent = t('failed'); return null; }
      return r.json();
    }).then(function (d) {
      if (!d) return;
      input.value = '';
      // 自己发的立刻飘出来, 不等下一轮轮询 —— 发完看不见等于"没发出去"。
      if (!seen[d.id]) { seen[d.id] = 1; if (d.t > since) since = d.t; fly(d); }
      hint.textContent = d.replied ? t('replied') : t('sent');
    }).catch(function () { hint.textContent = t('failed'); })
      .then(function () { send.disabled = false; });
  }

  if (send) send.addEventListener('click', post);
  if (input) {
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); post(); }
    });
  }

  // 起步时不把历史全部倒出来 —— 一进直播间被几十条糊满屏是灾难。
  // 只从"现在"开始收。
  fetch('/api/live/comments?limit=1&room=' + encodeURIComponent(room()), { credentials: 'same-origin' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { if (d) since = d.now || 0; })
    .catch(function () {})
    .then(function () { setInterval(pull, 4000); });

  return { pull: pull };
})();
