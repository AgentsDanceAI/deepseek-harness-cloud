/* DSH Cloud workspace chrome.
 *
 * Two jobs, both about the seam between our site and dsh's full-screen UI:
 *
 *  1. AN EXIT. dsh takes over the whole viewport and has no notion of our
 *     console, so entering the workspace was a one-way door. This adds one
 *     floating control with the ways out (console, port preview, stop, sign
 *     out) plus a live read of the free-hours allowance.
 *
 *  2. THE TASK HANDOFF. The marketing homepage has a composer; whatever the
 *     visitor typed there arrives as ?task= (and a sessionStorage copy that
 *     survives the login round-trip). We type it into dsh's own composer so
 *     the first prompt lands without the person retyping it.
 *
 * Injected by /api/work/shell into the container's document. dsh itself is
 * untouched, so this survives upstream updates.
 */
(function () {
  'use strict';

  var SITE = 'https://dshcloud.online';
  var TASK_KEY = 'dhc.pending_task';

  /* ---------------------------------------------------------------- exit */

  function buildChrome() {
    if (document.getElementById('dhc-exit-root')) return;

    var root = document.createElement('div');
    root.id = 'dhc-exit-root';
    root.innerHTML =
      '<div id="dhc-sheet" hidden role="menu" aria-label="DSH Cloud">' +
        '<div class="dhc-meta" id="dhc-meta">云工作台</div>' +
        '<a href="' + SITE + '/console" role="menuitem">← 返回控制台</a>' +
        '<a href="' + SITE + '/admin" id="dhc-admin" hidden role="menuitem">用户与额度管理</a>' +
        '<a href="' + SITE + '/preview" target="_blank" rel="noopener" role="menuitem">端口预览</a>' +
        '<button type="button" id="dhc-stop" role="menuitem">暂停工作台（省积分）</button>' +
        '<button type="button" id="dhc-signout" class="dhc-danger" role="menuitem">退出登录</button>' +
      '</div>' +
      '<button type="button" id="dhc-exit-btn" aria-haspopup="menu" aria-expanded="false">' +
        '<span class="dhc-dot" aria-hidden="true"></span><span>DSH Cloud</span>' +
      '</button>';
    document.body.appendChild(root);

    var btn = root.querySelector('#dhc-exit-btn');
    var sheet = root.querySelector('#dhc-sheet');

    function setOpen(open) {
      sheet.hidden = !open;
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) refreshMeta();
    }
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      setOpen(sheet.hidden);
    });
    document.addEventListener('click', function (e) {
      if (!sheet.hidden && !root.contains(e.target)) setOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !sheet.hidden) setOpen(false);
    });

    root.querySelector('#dhc-stop').addEventListener('click', function () {
      fetch(SITE + '/api/work/stop', { method: 'POST', credentials: 'include' })
        .catch(function () {})
        .then(function () { location.href = SITE + '/console'; });
    });
    root.querySelector('#dhc-signout').addEventListener('click', function () {
      fetch(SITE + '/api/auth/logout', { method: 'POST', credentials: 'include' })
        .catch(function () {})
        .then(function () { location.href = SITE + '/'; });
    });
  }

  /** Show what the workspace is costing: free hours left, else the balance. */
  function refreshMeta() {
    var meta = document.getElementById('dhc-meta');
    if (!meta) return;
    fetch(SITE + '/api/work/status', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (typeof s.free_minutes_left === 'number' && s.free_minutes_left > 0) {
          var h = Math.floor(s.free_minutes_left / 60), m = s.free_minutes_left % 60;
          meta.innerHTML = '免费时长剩余 <b>' + (h ? h + ' 小时 ' : '') + m + ' 分钟</b>';
        } else if (typeof s.balance === 'number') {
          meta.innerHTML = '积分余额 <b>' + s.balance + '</b>';
        } else {
          meta.textContent = '云工作台';
        }
      })
      .catch(function () {});
  }

  /* ----------------------------------------------------------- task handoff */

  function pendingTask() {
    var q = new URLSearchParams(location.search).get('task');
    if (q) return q;
    try { return sessionStorage.getItem(TASK_KEY) || ''; } catch (e) { return ''; }
  }

  function clearTask() {
    try { sessionStorage.removeItem(TASK_KEY); } catch (e) {}
    if (location.search.indexOf('task=') !== -1) {
      var url = new URL(location.href);
      url.searchParams.delete('task');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
    }
  }

  /** dsh's composer is the one editable textarea that is not the workspace picker. */
  function findComposer() {
    var areas = document.querySelectorAll('textarea');
    for (var i = 0; i < areas.length; i++) {
      var el = areas[i];
      if (el.readOnly || el.disabled) continue;
      var ph = (el.getAttribute('placeholder') || '');
      if (ph.indexOf('workspace') !== -1 || ph.indexOf('工作区') !== -1) continue;
      if (el.offsetParent === null) continue;
      return el;
    }
    return null;
  }

  /** React-controlled inputs ignore a plain value assignment. */
  function setValue(el, text) {
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) desc.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function deliverTask() {
    var task = pendingTask();
    if (!task) return true;
    var box = findComposer();
    if (!box) return false;
    box.focus();
    setValue(box, task);
    clearTask();
    return true;
  }

  /* -------------------------------------------------------------- bootstrap */

  function start() {
    buildChrome();
    // dsh boots asynchronously; poll briefly for its composer, then give up
    // quietly (the text stays in the box for the person to send themselves).
    var tries = 0;
    var timer = setInterval(function () {
      if (deliverTask() || ++tries > 40) clearInterval(timer);
    }, 500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
