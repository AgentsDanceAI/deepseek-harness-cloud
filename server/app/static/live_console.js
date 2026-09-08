/* 直播间控制台 (管理员)。播放器在 live.js 里, 这里只管配置。
 *
 * 数字人是**实时**说的 —— 没有"渲染"这一步: 保存下去, 下一轮当场就换了词。
 * 生成话术仍然只填进输入框、**不保存**: 它会被数字人当众念出去, 得有人过一眼。
 */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var name = $('lvName'), person = $('lvPerson'), voice = $('lvVoice');
  var script = $('lvScript'), counts = $('lvCounts'), say = $('lvSay');
  var recast = $('lvRecast');
  var loaded = { person: '', voice: '' }, optionsFilled = false;

  function t(el, k) { return (el.dataset || {})[k] || ''; }
  function lines() {
    return script.value.split('\n').map(function (x) { return x.trim(); })
      .filter(function (x) { return x.length; });
  }
  function busy(on) {
    ['lvSave', 'lvStart', 'lvStop', 'lvGen'].forEach(function (id) {
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
    counts.textContent = n === 0 ? '' : t(counts, 'fmt').replace('{n}', n);
    $('lvStop').hidden = !d.live;
    $('lvStart').hidden = !!d.live;
    if (d.live) {
      // starved = 队列被抽空的次数。不为零就是生成跟不上播出, 观众那边会卡 ——
      // 这个数必须露在页面上, 否则只有观众知道, 我们这边一切正常。
      var msg = t(counts, 'said').replace('{n}', d.said || 0);
      if (d.starved) msg += ' · ' + t(say, 'starved').replace('{n}', d.starved);
      say.textContent = msg;
    } else if (d.err) {
      say.textContent = t(say, 'failed') + ' ' + d.err;
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
  // 开播前**先存** —— 否则播的是上一版, 而画面看起来一切正常, 只是说的还是旧词。
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
