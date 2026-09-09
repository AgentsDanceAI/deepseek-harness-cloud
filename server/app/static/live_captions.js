/* 直播字幕: 画面右侧半透明滚动条。观看页与控制台共用。
 *
 * 为什么要有它: 观众可能静音看 (地铁上、办公室里), 也可能中途进来 —— 没有字幕
 * 就完全不知道她在说什么, 而这是一场"她一直在说话"的直播。
 *
 * 两个刻意的选择:
 *  1. 只追加不重排。每次轮询把新的几句接到底部, 已经在上面的一个字都不动 ——
 *     整块重画会让正在读的那行跳走。
 *  2. 服务端已经缓存了两秒 (见 live.py 的 _CAP_CACHE), 所以这里放心三秒一问:
 *     一百个观众打到 GPU 上的仍然是每两秒一次。
 */
window.LiveCaptions = (function () {
  var video = document.getElementById('lvVideo');
  var stage = video && video.parentNode;
  var box = document.getElementById('lvCaps');
  var toggle = document.getElementById('lvCapsToggle');
  if (!box || !stage) return null;

  var seen = {}, timer = null;
  var KEY = 'dhc.live.captions';

  function on() {
    try { return localStorage.getItem(KEY) !== 'off'; } catch (e) { return true; }
  }
  function paintToggle() {
    box.hidden = !on();
    if (toggle) {
      toggle.textContent = toggle.dataset[on() ? 'hide' : 'show'] || '';
      toggle.setAttribute('aria-pressed', on() ? 'true' : 'false');
    }
  }

  function add(line) {
    var id = line.t + '|' + line.text;
    if (seen[id]) return;
    seen[id] = 1;
    var el = document.createElement('div');
    el.className = 'lv-cap' + (line.kind === 'interject' ? ' lv-cap--in' : '');
    el.textContent = line.text;
    box.appendChild(el);
    while (box.children.length > 14) box.removeChild(box.firstChild);
    // 贴着底部 —— 最新一句永远在视线落点上
    box.scrollTop = box.scrollHeight;
  }

  function pull() {
    return fetch('/api/live/captions', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (!d.live) {                 // 停播了就清空, 别把上一场的字留在屏幕上
          box.textContent = ''; seen = {};
          return;
        }
        (d.lines || []).forEach(add);
      })
      .catch(function () {});
  }

  if (toggle) {
    toggle.addEventListener('click', function () {
      try { localStorage.setItem(KEY, on() ? 'off' : 'on'); } catch (e) { /* 无痕窗口 */ }
      paintToggle();
    });
  }
  paintToggle();
  pull();
  timer = setInterval(pull, 3000);
  return { pull: pull, stop: function () { clearInterval(timer); } };
})();
