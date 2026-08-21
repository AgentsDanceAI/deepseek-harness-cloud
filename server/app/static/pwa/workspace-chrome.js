/* deepseek-harness-cloud workspace chrome.
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

  /* --------------------------------------------------- 手机: 点抽屉外收起它 */

  /* 第一版把遮罩做成 sidebarCol 的 ::after, 并假设点击会落在侧栏元素上 —— 两条都
     错了。dsh 的 .pI_x6G_sidebarCol 和它的父容器 .pI_x6G_frame **都是
     overflow:hidden**, 而遮罩定位在 left:100% (侧栏之外), 直接被裁掉, 从来没
     渲染过; 于是点右边时事件目标是聊天区, 处理函数第一步就返回了。

     现在分成两件独立的事, 关键的那件不依赖另一件:
       行为 — document 上的点击处理: 只要落点不在侧栏里就收起。不碰遮罩,
              所以遮罩渲不渲染都不影响。
       观感 — 我们自己挂在 body 上的遮罩层 (不在 dsh 那两个 overflow:hidden
              容器里, 裁不掉), 并且**贴着抽屉右边**放, 万一层级判断有误也绝不会
              盖住抽屉本身。

     只在窄屏生效 —— 桌面端点聊天区收起侧栏是错的。760px 与 mobile.css 同一个断点。 */

  var NARROW = '(max-width: 760px)';

  function sidebarEl() { return document.querySelector('[class*="sidebarCol"]'); }
  function sidebarOpen(col) {
    return !!col && !col.querySelector('[class*="collapsed"]');
  }

  function collapseSidebar(col) {
    var toggle = col.querySelector('[class*="_toggle"]');
    if (toggle) toggle.click();
  }

  function scrimEl() {
    var el = document.getElementById('dhc-scrim');
    if (!el) {
      el = document.createElement('div');
      el.id = 'dhc-scrim';
      el.addEventListener('click', function () {
        var col = sidebarEl();
        if (col) collapseSidebar(col);
      });
      document.body.appendChild(el);
    }
    return el;
  }

  function syncScrim() {
    var col = sidebarEl();
    var el = scrimEl();
    if (!col || !window.matchMedia(NARROW).matches || !sidebarOpen(col)) {
      el.style.display = 'none';
      return;
    }
    // 贴着抽屉右边: 即使层级判断有误, 也不可能盖住抽屉
    el.style.left = Math.round(col.getBoundingClientRect().right) + 'px';
    el.style.display = 'block';
  }

  /* 落点不在侧栏里 -> 收起。**不 preventDefault**: 点输入框那一下要既收抽屉又
     聚焦输入框, 吞掉它会让人以为没反应。 */
  function tapOutsideSidebar(e) {
    if (!window.matchMedia(NARROW).matches) return;
    var col = sidebarEl();
    if (!col || !sidebarOpen(col)) return;
    if (col.contains(e.target)) return;
    collapseSidebar(col);
  }

  function watchSidebar() {
    document.addEventListener('click', tapOutsideSidebar, true);
    window.addEventListener('resize', syncScrim);
    // dsh 收起/展开时改的是 class, 用 MutationObserver 跟住; 另加一个低频兜底,
    // 免得它换成别的机制时遮罩永远停在错的状态。
    var mo = new MutationObserver(syncScrim);
    mo.observe(document.body, { subtree: true, attributes: true,
                                attributeFilter: ['class'] });
    setInterval(syncScrim, 1000);
    syncScrim();
  }

  /* ---------------------------------------------------------------- exit */

  function buildChrome() {
    if (document.getElementById('dhc-exit-root')) return;

    var root = document.createElement('div');
    root.id = 'dhc-exit-root';
    root.innerHTML =
      '<div id="dhc-sheet" hidden role="menu" aria-label="deepseek-harness-cloud">' +
        '<div class="dhc-meta" id="dhc-meta">云工作台</div>' +
        '<a href="' + SITE + '/console" role="menuitem">← 返回控制台</a>' +
        '<a href="' + SITE + '/console/admin" id="dhc-admin" hidden role="menuitem">用户与额度管理</a>' +
        '<a href="' + SITE + '/preview" target="_blank" rel="noopener" role="menuitem">个人成品</a>' +
        '<button type="button" id="dhc-stop" role="menuitem">暂停工作台（省积分）</button>' +
        '<button type="button" id="dhc-signout" class="dhc-danger" role="menuitem">退出登录</button>' +
      '</div>' +
      '<button type="button" id="dhc-exit-btn" aria-haspopup="menu" aria-expanded="false">' +
        '<span class="dhc-dot" aria-hidden="true"></span><span>deepseek-harness-cloud</span>' +
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
    // 捕获阶段绑定: 抽屉里的项自己会 stopPropagation, 冒泡阶段收不到遮罩那一下。
    watchSidebar();
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
