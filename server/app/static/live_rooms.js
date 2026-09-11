/* 直播间列表: 把每张卡点亮成"直播中 / 未开播"。
 *
 * 状态不在服务端渲染 —— 那要挨个问 GPU 节点, 而它是别人的共享机, 够不着是常态。
 * 让页面等在上游身上的表现是: 上游一抖, 整页白着。所以壳先出来, 状态后到。
 */
(function () {
  var grid = document.getElementById('lvRooms');
  if (!grid) return;
  var d = grid.dataset;

  function paint(rooms) {
    var byId = {};
    rooms.forEach(function (r) { byId[r.id] = r; });
    Array.prototype.forEach.call(grid.querySelectorAll('.lv-roomcard'), function (card) {
      var r = byId[card.dataset.room];
      card.classList.remove('is-loading');
      if (!r) return;
      // 名称由管理员在控制台里配, 没配过就先显示房间 id —— 总比空着强。
      card.querySelector('.lv-roomname').textContent = r.title || card.dataset.room;
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
