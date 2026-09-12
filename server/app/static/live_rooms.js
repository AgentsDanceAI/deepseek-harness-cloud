/* 直播间列表: 把每张卡点亮成"直播中 / 未开播"。
 *
 * 状态不在服务端渲染 —— 那要挨个问 GPU 节点, 而它是别人的共享机, 够不着是常态。
 * 让页面等在上游身上的表现是: 上游一抖, 整页白着。所以壳先出来, 状态后到。
 */
(function () {
  var grid = document.getElementById('lvRooms');
  if (!grid) return;
  var d = grid.dataset;
  var T = window.__T || {};

  // 封面取不到 (这间还没起过, 上游没有底图) 就把 img 收起来, 露出那块底色 ——
  // 别让浏览器画一个碎图标。
  Array.prototype.forEach.call(grid.querySelectorAll('.lv-roomcover img'), function (img) {
    img.addEventListener('error', function () { img.classList.add('is-broken'); });
  });

  function put(el, text) {
    // 只在真的变了的时候写: 无谓地重写 textContent 会把用户选中的文字清掉,
    // 而这一页每十五秒刷一次。
    if (el && text && el.textContent !== text) el.textContent = text;
  }

  function paint(rooms) {
    var byId = {};
    rooms.forEach(function (r) { byId[r.id] = r; });
    Array.prototype.forEach.call(grid.querySelectorAll('.lv-roomcard'), function (card) {
      var r = byId[card.dataset.room];
      card.classList.remove('is-loading');
      if (!r) return;
      // 标题/主播名服务端已经用上一次拿到的渲染过一轮 (见 live.cards_hint),
      // 这里是把它们更到最新。**拿不到标题就别写** —— 回落成房间 id 就是
      // "先英文后中文"那个毛病。
      put(card.querySelector('.lv-roomname'), r.title);
      put(card.querySelector('.lv-roomanchor'), r.preset && T['js.avatar.p.' + r.preset]);
      var st = card.querySelector('.lv-roomstate');
      st.textContent = r.live ? d.live : d.off;
      st.className = 'lv-roomstate ' + (r.live ? 'is-live' : 'is-off');
      card.classList.toggle('is-live', !!r.live);
    });
  }

  function pull() {
    return fetch('/api/live/rooms', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j && j.rooms) paint(j.rooms); })
      .catch(function () {
        // 拿不到就把 loading 摘掉, 卡片仍然点得进去 —— 进去之后播放器自己会重试。
        Array.prototype.forEach.call(grid.querySelectorAll('.lv-roomcard'), function (c) {
          c.classList.remove('is-loading');
        });
      });
  }
  pull();
  setInterval(pull, 15000);
})();
