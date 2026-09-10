/* 直播间控制台 (管理员)。播放器在 live.js 里, 这里只管配置。
 *
 * 数字人是**实时**说的 —— 没有"渲染"这一步: 保存下去, 下一轮当场就换了词。
 * 生成话术仍然只填进输入框、**不保存**: 它会被数字人当众念出去, 得有人过一眼。
 */
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var name = $('lvName'), preset = $('lvPreset');
  var script = $('lvScript'), counts = $('lvCounts'), say = $('lvSay');
  var recast = $('lvRecast');
  var loaded = { person: '', voice: '' }, presetInit = false;
  var pending = '';   // 'start' = 已经点了开播, 还在等上游真的出流
  var comment = $('lvComment'), mode = $('lvMode'), log = $('lvLog');

  function t(el, k) { return (el.dataset || {})[k] || ''; }
  function lines() {
    return script.value.split('\n').map(function (x) { return x.trim(); })
      .filter(function (x) { return x.length; });
  }
  function busy(on) {
    ['lvSave', 'lvStart', 'lvStop', 'lvGen', 'lvSend'].forEach(function (id) {
      var el = $(id); if (el) el.disabled = on;
    });
  }
  /* 形象与音色是**一对**, 选项由服务端渲染 (见 live.py 的 LIVE_PRESETS), 搭配挂在
     option 的 data-* 上。前端不认识任何形象 id —— 加形象只改后端那张表。 */
  function pick() {
    var o = preset.options[preset.selectedIndex];
    return o ? { person: o.dataset.person || '', voice: o.dataset.voice || '' }
             : { person: '', voice: '' };
  }
  /* 存量房间可能存着自由搭配的组合 (旧控制台两个下拉各选各的), 所以退让顺序要和
     后端 preset_of 一致: 整对精确匹配 → 只按形象匹配 → 第一个。**绝不留空**, 空的
     下拉会让保存把形象和音色一起清掉。 */
  function selectPreset(p, v) {
    var byPerson = -1;
    for (var i = 0; i < preset.options.length; i++) {
      var o = preset.options[i];
      if (o.dataset.person === p && o.dataset.voice === v) { preset.selectedIndex = i; return; }
      if (byPerson < 0 && o.dataset.person === p) byPerson = i;
    }
    preset.selectedIndex = byPerson >= 0 ? byPerson : 0;
  }

  /* 换形象/音色的提示。原先写的是"全部话术都要重新渲染" —— 那是**上一版预渲染
     设计**的说法, 现在数字人是实时说的, 存下去下一句就换了, 没有重渲这回事。
     留着这条会让人以为改一下要等二十分钟, 于是不敢改。 */
  function markRecast() {
    var c = pick();
    recast.hidden = !(loaded.person && (c.person !== loaded.person || c.voice !== loaded.voice));
  }

  /* 产出速率。**低于 1.0 = 观众必卡**, 而且换多大的缓冲都没用 —— 生产比消费慢,
     缓冲只是决定多久被抽干。这一行是控制台上唯一能回答"现在卡不卡"的东西。
     2026-09-10 加: 形象下拉只有名字, 看不出哪个走云端 TTS(实测 2.4×)、哪个走自建
     (0.7×), 于是"我明明换了却还卡"这种误会没法自己排除。 */
  var rateEl = $('lvRate');
  function paintRate(d) {
    if (!rateEl) return;
    var r = +(d && d.rate) || 0;
    if (!d || !d.live || !r) { rateEl.hidden = true; return; }
    rateEl.hidden = false;
    var slow = r < 0.98;
    rateEl.className = 'lv-rate' + (slow ? ' lv-rate--slow' : '');
    rateEl.textContent = t(rateEl, slow ? 'slow' : 'ok').replace('{r}', r.toFixed(2));
  }

  function paint(d) {
    if (document.activeElement !== name) name.value = d.title || '';
    if (document.activeElement !== script) script.value = (d.lines || []).join('\n');
    // 只在首次落位 —— 之后 15 秒一次的刷新不能把没保存的改动顶回去。
    if (!presetInit) { presetInit = true; selectPreset(d.person || '', d.voice || ''); }
    loaded = { person: d.person || '', voice: d.voice || '' };
    paintRate(d);
    var n = (d.lines || []).length;
    counts.textContent = n === 0 ? '' : t(counts, 'fmt').replace('{n}', n);
    // 上游接了 start 就返回, 真正出流要几秒到几十秒 (要先生成第一句)。这中间
    // 状态还是 live:false —— 原先按钮就此复原, 用户看到的是"点了没反应", 于是
    // 再点一次, 再点一次。所以自己记一个"正在开播", 直到上游真的 live。
    if (d.live) pending = '';
    $('lvStop').hidden = !d.live;
    $('lvStart').hidden = !!d.live;
    $('lvStart').disabled = pending === 'start';
    if (pending === 'start' && !d.live) say.textContent = t(say, 'starting');
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
    paintLog(d.recent || []);
  }

  function paintLog(items) {
    if (!log) return;
    log.textContent = '';
    items.slice().reverse().forEach(function (x) {
      var row = document.createElement('div');
      row.className = 'lv-logrow' + (x.kind === 'interject' ? ' lv-logrow--in' : '');
      var tag = document.createElement('span');
      tag.className = 'lv-tag';
      tag.textContent = t(log, x.kind === 'interject' ? 'interject' : 'script');
      row.appendChild(tag);
      row.appendChild(document.createTextNode(x.text || ''));
      log.appendChild(row);
    });
  }

  function refresh() {
    return fetch('/api/live/room', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) paint(d); if (window.LivePlayer) window.LivePlayer.refresh(); })
      .catch(function () {});
  }

  /* 换了搭配之后自己把播放接回来 —— 别让人去点"点一下开始播放"。
   *
   * 换搭配时上游是 stop+start(见 live_server 的 put_room): 播放列表被 rmtree 重建,
   * 手里缓冲的切片全 404。播放器靠 since 认出"换了一场"并重建(见 live.js), 但那
   * 是 15 秒一次的常规轮询, 中间这十几秒画面就是黑的, 而且第一句还要生成几秒。
   * 所以这里主动催: 3 秒一次问到它真的换了场并出流为止。
   * 自动播放不会被拒 —— 用户刚点过「保存」, 页面已经有过用户手势。 */
  function resumeAfterRecast() {
    var tries = 0;
    (function tick() {
      if (++tries > 20) return;                  // 一分钟还没回来就算了
      setTimeout(function () {
        if (window.LivePlayer) window.LivePlayer.refresh();
        tick();
      }, 3000);
    })();
  }

  function save() {
    var was = { person: loaded.person, voice: loaded.voice };
    var now = pick();
    var recasting = !!(was.person && (now.person !== was.person || now.voice !== was.voice));
    busy(true);
    return fetch('/api/live/room', {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: name.value, person: pick().person, voice: pick().voice, lines: lines(),
      }),
    }).then(function (r) {
      if (!r.ok) throw new Error('save');
      say.textContent = t(say, 'saved');
    }).catch(function () { say.textContent = t(say, 'failed'); })
      .then(function () {
        busy(false);
        if (recasting) resumeAfterRecast();
        return refresh();
      });
  }

  function act(a, word) {
    busy(true);
    pending = a === 'start' ? 'start' : '';
    say.textContent = t(say, word);
    return fetch('/api/live/room/' + a, { method: 'POST', credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error(a); })
      .catch(function () { pending = ''; say.textContent = t(say, 'failed'); })
      .then(function () { busy(false); return refresh(); })
      .then(function () { if (pending === 'start') pollUntilLive(); });
  }

  /* 开播后盯着上游, 直到真的出流。15 秒一次的常规轮询太慢 —— 第一句生成通常
     十几秒, 而这段时间页面上什么都不动, 看起来就是没反应。 */
  function pollUntilLive() {
    var tries = 0;
    (function tick() {
      if (pending !== 'start') return;
      if (++tries > 40) {                      // 2 分钟还没出流就别装了
        pending = '';
        say.textContent = t(say, 'start_slow');
        return;
      }
      setTimeout(function () { refresh().then(tick); }, 3000);
    })();
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

  function send() {
    var text = comment.value.trim();
    if (!text) { comment.focus(); return; }
    busy(true);
    say.textContent = t(say, 'sending');
    fetch('/api/live/say', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, mode: mode.value }),
    }).then(function (r) { if (!r.ok) throw new Error('say'); return r.json(); })
      .then(function (d) {
        comment.value = '';
        // 把她将要说的原话回显出来 —— 模式是"让她回答"时, 说出去的和你敲进去的
        // 不是一回事, 不回显就只能等十秒听见了才知道她答了什么。
        say.textContent = t(say, 'queued') + ' ' + (d.spoken || '');
      })
      .catch(function () { say.textContent = t(say, 'failed'); })
      .then(function () { busy(false); });
  }
  $('lvSend').addEventListener('click', send);
  // Ctrl/Cmd+Enter 发送 —— 回评论是要抢时间的, 手别离开键盘。
  comment.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); send(); }
  });
  $('lvGen').addEventListener('click', generate);
  $('lvSave').addEventListener('click', function () { save(); });
  // 开播前**先存** —— 否则播的是上一版, 而画面看起来一切正常, 只是说的还是旧词。
  $('lvStart').addEventListener('click', function () { save().then(function () { act('start', 'starting'); }); });
  $('lvStop').addEventListener('click', function () { pending = ''; act('stop', 'stopped'); });
  preset.addEventListener('change', markRecast);
  script.addEventListener('input', function () {
    var n = lines().length;
    counts.textContent = n ? t(counts, 'dirty').replace('{n}', n) : '';
  });

  refresh();
  setInterval(refresh, 15000);
})();
