/* 数字人直播间: 控制台 + 预览。
 *
 * 三个必须讲清楚的坑:
 *  1. 自动播放策略只放行**静音**起播。所以 video 标签自带 muted, 出声靠按钮
 *     (那一下有用户手势)。不这么做的表现是整页安静地什么都不发生, 只有控制台里
 *     一行 NotAllowedError。
 *  2. 直播流会断 (切片轮转、网络抖动、上游重启)。hls.js 的致命错误必须自己接,
 *     否则播放器停在最后一帧, 而**看上去和"主播不说话"一模一样**。
 *  3. 保存 ≠ 渲染 ≠ 开播, 是三步。渲染要占 GPU 好几分钟, 不能因为用户改了个
 *     错别字就自动整场重来。
 */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var v = $('lvVideo'), msg = $('lvMsg'), badge = $('lvBadge'), unmute = $('lvUnmute');
  var con = $('lvConsole'), name = $('lvName'), person = $('lvPerson'), voice = $('lvVoice');
  var script = $('lvScript'), counts = $('lvCounts'), say = $('lvSay');
  var bar = $('lvBar'), progress = $('lvProgress'), recast = $('lvRecast');
  var watching = $('lvWatching');
  var hls = null, retry = 0, playingUrl = '', room = null, poll = null;
  var loaded = { person: '', voice: '', lines: [] };

  function t(el, k) { return (el.dataset || {})[k] || ''; }
  function note(s) { msg.textContent = s || ''; msg.hidden = !s; }
  function online(on) {
    badge.textContent = t(badge, on ? 'on' : 'off');
    badge.className = 'lv-badge lv-badge--' + (on ? 'on' : 'off');
  }
  function lines() {
    return script.value.split('\n').map(function (x) { return x.trim(); })
      .filter(function (x) { return x.length; });
  }

  // ── 播放 ───────────────────────────────────────────────────────────
  function play(url, mine) {
    watching.textContent = t(watching, mine ? 'mine' : 'official');
    if (url === playingUrl && hls && !v.paused) return;
    playingUrl = url;
    if (hls) { hls.destroy(); hls = null; }
    if (v.canPlayType('application/vnd.apple.mpegurl')) {   // Safari 原生放 HLS
      v.src = url; v.play().catch(function () {}); return;
    }
    if (!window.Hls || !window.Hls.isSupported()) { note(t(badge, 'unsupported')); return; }
    hls = new window.Hls({ lowLatencyMode: true, liveSyncDurationCount: 3 });
    hls.loadSource(url);
    hls.attachMedia(v);
    hls.on(window.Hls.Events.MANIFEST_PARSED, function () { v.play().catch(function () {}); });
    hls.on(window.Hls.Events.ERROR, function (_e, d) {
      if (!d.fatal) return;
      if (d.type === window.Hls.ErrorTypes.NETWORK_ERROR) { hls.startLoad(); return; }
      if (d.type === window.Hls.ErrorTypes.MEDIA_ERROR) { hls.recoverMediaError(); return; }
      retry += 1; playingUrl = '';
      note(t(badge, 'reconnect'));
      setTimeout(refresh, Math.min(30000, 2000 * retry));
    });
  }

  // ── 状态 ───────────────────────────────────────────────────────────
  function refresh() {
    var mine = room ? fetch('/api/live/room', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
      : Promise.resolve(null);
    var off = fetch('/api/live/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); }).catch(function () { return { live: false }; });
    return Promise.all([mine, off]).then(function (a) {
      var me = a[0], o = a[1];
      if (me) paintConsole(me);
      // 我的间在播就看我的, 否则看官方间 —— 页面永远不是空的。
      if (me && me.live) { online(true); note(''); retry = 0; play(me.hls, true); }
      else if (o.live) { online(!!(me && me.live)); note(''); retry = 0; play(o.hls, false); }
      else { online(false); note(t(badge, o.enabled === false ? 'disabled' : 'notlive')); }
    });
  }

  function paintConsole(d) {
    con.hidden = false;
    if (document.activeElement !== name) name.value = d.title || '';
    if (document.activeElement !== script) script.value = (d.lines || []).join('\n');
    fillOptions(d);
    loaded = { person: d.person || '', voice: d.voice || '', lines: (d.lines || []).slice() };
    var n = (d.lines || []).length;
    var done = (d.rendered || []).filter(Boolean).length;
    // 文案模板从 data-* 来 (这一页别的地方也这么做) —— 把中文写死在 JS 里,
    // 这一页就永远只有一种语言, 而且翻译的人找不到它。
    counts.textContent = n === 0 ? '' :
      t(counts, 'fmt').replace('{n}', n).replace('{done}', done).replace('{todo}', n - done);
    $('lvStop').hidden = !d.live;
    $('lvStart').hidden = !!d.live;
    var r = d.render || {};
    if (r.total && r.done < r.total) {
      progress.hidden = false;
      bar.style.width = Math.round(100 * r.done / r.total) + '%';
      say.textContent = t(say, 'rendering') + ' ' + r.done + '/' + r.total;
      if (!poll) poll = setInterval(refresh, 4000);
    } else {
      progress.hidden = true;
      if (poll) { clearInterval(poll); poll = null; }
      if (r.err) say.textContent = t(say, 'failed') + ' ' + r.err;
    }
    // 改了形象/音色 = 全部片段作废 (缓存键是三元组), 提前说破。
    recast.hidden = !(loaded.person && (person.value !== loaded.person || voice.value !== loaded.voice));
  }

  var optionsFilled = false;
  function fillOptions(d) {
    if (optionsFilled) {
      if (!person.value) person.value = d.person || '';
      if (!voice.value) voice.value = d.voice || '';
      return;
    }
    optionsFilled = true;
    fetch('/api/avatar/config', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (c) {
        if (!c) { optionsFilled = false; return; }
        (c.persons || []).forEach(function (p) {
          var id = typeof p === 'string' ? p : (p.id || p.name);
          var o = document.createElement('option');
          o.value = id; o.textContent = (typeof p === 'object' && p.name) ? p.name : id;
          person.appendChild(o);
        });
        (c.voices || []).forEach(function (x) {
          var o = document.createElement('option');
          o.value = x.id; o.textContent = x.name || x.id;
          voice.appendChild(o);
        });
        person.value = d.person || c.person_default || '';
        voice.value = d.voice || c.voice_default || '';
      })
      .catch(function () { optionsFilled = false; });
  }

  // ── 动作 ───────────────────────────────────────────────────────────
  function busy(on) {
    ['lvSave', 'lvRender', 'lvStart', 'lvStop'].forEach(function (id) { $(id).disabled = on; });
  }
  function save() {
    busy(true);
    return fetch('/api/live/room', {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: name.value, person: person.value, voice: voice.value, lines: lines() }),
    }).then(function (r) {
      if (!r.ok) throw new Error('save');
      say.textContent = t(say, 'saved');
    }).catch(function () { say.textContent = t(say, 'failed'); })
      .then(function () { busy(false); return refresh(); });
  }
  function act(a, word) {
    busy(true);
    say.textContent = t(say, word);
    return fetch('/api/live/room/' + a, { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error(a); })
      .catch(function () { say.textContent = t(say, 'failed'); })
      .then(function () { busy(false); return refresh(); });
  }

  $('lvSave').addEventListener('click', function () { save(); });
  // 渲染前**先存** —— 否则用户改完直接点渲染, 渲的还是上一版, 而画面看起来一切
  // 正常, 只是说的还是旧词。
  $('lvRender').addEventListener('click', function () { save().then(function () { act('render', 'rendering'); }); });
  $('lvStart').addEventListener('click', function () { save().then(function () { act('start', 'starting'); }); });
  $('lvStop').addEventListener('click', function () { act('stop', 'stopped'); });
  [person, voice].forEach(function (el) {
    el.addEventListener('change', function () {
      recast.hidden = !(loaded.person && (person.value !== loaded.person || voice.value !== loaded.voice));
    });
  });
  script.addEventListener('input', function () {
    var n = lines().length;
    counts.textContent = n ? t(counts, 'dirty').replace('{n}', n) : '';
  });

  v.addEventListener('playing', function () {
    note('');
    if (v.muted) unmute.hidden = false;   // 起播了才提示开声音
  });
  unmute.addEventListener('click', function () {
    v.muted = false; v.volume = 1; unmute.hidden = true; v.play().catch(function () {});
  });

  // 先问一次"我有没有直播间", 有就把控制台亮出来。
  fetch('/api/live/room', { credentials: 'same-origin' })
    .then(function (r) { if (r.ok) return r.json(); return null; })
    .then(function (d) { if (d) { room = d.room; paintConsole(d); } })
    .catch(function () {})
    .then(function () { refresh(); setInterval(refresh, 15000); });

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && (!hls || v.paused)) refresh();
  });
})();
