/* 直播间控制台 (管理员)。播放器在 live.js 里, 这里只管配置。
 *
 * 保存 ≠ 生成 ≠ 渲染 ≠ 开播, 四步刻意分开:
 *  · 生成只填进输入框, **不保存** —— 生成的话术会被数字人当众念出去, 得有人过眼;
 *  · 渲染要占 GPU 好几分钟, 不能因为改了个错别字就自动整场重来;
 *  · 开播前会先保存, 否则播的是上一版, 而画面一切正常只是说的还是旧词。
 */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var name = $('lvName'), person = $('lvPerson'), voice = $('lvVoice');
  var script = $('lvScript'), counts = $('lvCounts'), say = $('lvSay');
  var bar = $('lvBar'), progress = $('lvProgress'), recast = $('lvRecast');
  var poll = null, loaded = { person: '', voice: '' }, optionsFilled = false;

  function t(el, k) { return (el.dataset || {})[k] || ''; }
  function lines() {
    return script.value.split('\n').map(function (x) { return x.trim(); })
      .filter(function (x) { return x.length; });
  }
  function busy(on) {
    ['lvSave', 'lvRender', 'lvStart', 'lvStop', 'lvGen'].forEach(function (id) {
      var el = $(id); if (el) el.disabled = on;
    });
  }
  function markRecast() {
    recast.hidden = !(loaded.person && (person.value !== loaded.person || voice.value !== loaded.voice));
  }

  function paint(d) {
    if (document.activeElement !== name) name.value = d.title || '';
    if (document.activeElement !== script) script.value = (d.lines || []).join('\n');
    fillOptions(d);
    loaded = { person: d.person || '', voice: d.voice || '' };
    var n = (d.lines || []).length;
    var done = (d.rendered || []).filter(Boolean).length;
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
    markRecast();
  }

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
        loaded = { person: person.value, voice: voice.value };
      })
      .catch(function () { optionsFilled = false; });
  }

  function refresh() {
    return fetch('/api/live/room', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) paint(d); if (window.LivePlayer) window.LivePlayer.refresh(); })
      .catch(function () {});
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

  function generate() {
    var topic = name.value.trim();
    if (!topic) { say.textContent = t(say, 'need_title'); name.focus(); return; }
    busy(true);
    say.textContent = t(say, 'generating');
    fetch('/api/live/generate', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: topic }),
    }).then(function (r) { if (!r.ok) throw new Error('gen'); return r.json(); })
      .then(function (d) {
        // 只填进框里, **不保存** —— 让人先看一眼。存不存由他按"保存"。
        script.value = (d.lines || []).join('\n');
        var n = lines().length;
        counts.textContent = n ? t(counts, 'dirty').replace('{n}', n) : '';
        say.textContent = t(say, 'generated');
      })
      .catch(function () { say.textContent = t(say, 'failed'); })
      .then(function () { busy(false); });
  }

  $('lvGen').addEventListener('click', generate);
  $('lvSave').addEventListener('click', function () { save(); });
  // 渲染/开播前**先存** —— 否则改完直接点, 渲的/播的还是上一版, 而画面看起来
  // 一切正常, 只是说的还是旧词。
  $('lvRender').addEventListener('click', function () { save().then(function () { act('render', 'rendering'); }); });
  $('lvStart').addEventListener('click', function () { save().then(function () { act('start', 'starting'); }); });
  $('lvStop').addEventListener('click', function () { act('stop', 'stopped'); });
  [person, voice].forEach(function (el) { el.addEventListener('change', markRecast); });
  script.addEventListener('input', function () {
    var n = lines().length;
    counts.textContent = n ? t(counts, 'dirty').replace('{n}', n) : '';
  });

  refresh();
  setInterval(refresh, 15000);
})();
