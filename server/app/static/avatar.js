/* 数字人通话页。
 *
 * 链路: 麦克风 16k PCM → duplug (语义话轮判定) → 定稿文本 → 我们的网关出回答
 *       → avatar 容器 (TTS + 逐块口型) → fMP4 → MSE 播放。
 *
 * 音画同步不用我们操心: avatar 侧把音频和视频打进**同一个 fMP4 容器**再推过来,
 * 浏览器解出来天然是同步的。我们只负责按顺序灌进 SourceBuffer。
 */
(function () {
  const $ = (s) => document.querySelector(s);
  if (!$("#avCall")) return;

  const T = (window.__T || {});
  const t = (k, d) => T[k] || d;

  const RT_CODEC = 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"';
  const st = {
    sess: null, cfg: null, ws: null, ear: null,
    ms: null, sb: null, queue: [], playing: false,
    t0: null, timer: null, rate: 0,
  };

  /* iPhone 没有标准 MediaSource — iOS 17.1+ 给的是同形的 ManagedMediaSource。
     两个都没有就只能退纯语音 (这页的意义就没了, 所以直接说清楚)。 */
  function mediaSource() {
    const w = window;
    if (w.MediaSource?.isTypeSupported?.(RT_CODEC)) return w.MediaSource;
    if (w.ManagedMediaSource?.isTypeSupported?.(RT_CODEC)) return w.ManagedMediaSource;
    return null;
  }

  function status(msg, bad) {
    const el = $("#avStatus");
    el.textContent = msg || "";
    el.className = "av-status" + (bad ? " av-bad" : "");
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  /* ---------- 启动: 拿令牌与形象/音色清单 ---------- */
  async function boot() {
    const s = await api("/api/avatar/session");
    if (s.status === 402) {
      status(t("avatar.no_credits", `积分不足，通话需要 ${s.d.credits_per_min} 积分/分钟`), true);
      $("#avCall").disabled = true;
      return;
    }
    if (!s.ok) { status(t("avatar.unavailable", "数字人暂时不可用"), true); return; }
    st.sess = s.d;
    st.rate = s.d.credits_per_min;
    $("#avBalance").textContent = s.d.balance;
    $("#avRate").textContent = `${s.d.credits_per_min} ${t("avatar.per_min", "积分/分钟")}`;

    // 形象与音色清单由 GPU 侧给, 且**已按租户过滤** — 别人上传的脸不会在这里。
    const c = await api(`/api/avatar/config`);
    if (!c.ok) { status(t("avatar.unavailable", "数字人暂时不可用"), true); return; }
    st.cfg = c.d;
    fill($("#avPerson"), c.d.persons || [], c.d.person_default);
    fill($("#avVoice"), (c.d.voices || []).map(v => v.id), c.d.voice_default,
         (c.d.voices || []).reduce((m, v) => (m[v.id] = v.name, m), {}));
    loadBg();
    layout();
  }

  /* 背景**跟着形象走**: 视频层是按这个形象的脸框贴上去的, 而上传形象的脸框是
     它自己那张图的坐标 —— 背景取错就是一张脸浮在不属于它的身体上。
     ver 记在每个形象名下: 同一个 id 重传一张新图时, 全局 bg_ver 不会变, 只靠
     它打不穿浏览器缓存 (会看到上一张脸)。 */
  const bgVer = {};
  function loadBg() {
    const p = $("#avPerson").value || "";
    const v = bgVer[p] || st.cfg?.bg_ver || 0;
    $("#avBg").src = `/api/avatar/bg.png?person=${encodeURIComponent(p)}&v=${v}`;
  }

  function fill(sel, ids, def, names) {
    sel.innerHTML = "";
    const o0 = document.createElement("option");
    o0.value = ""; o0.textContent = t("avatar.default", "默认") + (def ? `（${def}）` : "");
    sel.appendChild(o0);
    for (const id of ids) {
      const o = document.createElement("option");
      o.value = id; o.textContent = (names && names[id]) || id;
      sel.appendChild(o);
    }
  }

  /* 视频层要按 crop 贴回背景 —— 每个形象的 crop 不同, 用错了就是错位。 */
  function layout() {
    const crop = (st.cfg?.person_crops || {})[$("#avPerson").value] || st.cfg?.crop;
    if (!crop) return;
    const v = $("#avVideo");
    v.style.left = crop.x * 100 + "%";
    v.style.top = crop.y * 100 + "%";
    v.style.width = crop.w * 100 + "%";
    v.style.height = crop.h * 100 + "%";
  }
  // 换形象要**同时**换背景和重算视频层位置 —— 只换一个就是错位。
  $("#avPerson").addEventListener("change", () => { loadBg(); layout(); });

  /* ---------- 上传形象 ---------- */
  $("#avUpload").addEventListener("change", async (e) => {
    const f = e.target.files?.[0];
    e.target.value = "";                 // 同一张图连传两次也要触发
    if (!f || !st.sess) return;
    $("#avUploadText").textContent = t("avatar.uploading", "上传中…");
    const id = "p" + Date.now().toString(36);
    const r = await fetch(`/api/avatar/persons?id=${id}`, { method: "POST", body: f });
    const d = await r.json().catch(() => ({}));
    $("#avUploadText").textContent = t("avatar.upload", "传张照片");
    if (!r.ok) { status(d.error || t("avatar.upload_failed", "上传失败"), true); return; }
    // 服务端已经热加载好, 直接选中 —— 传完就能用, 不让人再点一次。
    st.cfg.persons = d.persons || st.cfg.persons;
    (st.cfg.person_crops = st.cfg.person_crops || {})[d.id] = d.crop;
    fill($("#avPerson"), st.cfg.persons, st.cfg.person_default);
    $("#avPerson").value = d.id;
    bgVer[d.id] = Date.now();            // 刚传的图, 别让缓存吐上一张
    loadBg();
    layout();
    status(t("avatar.upload_ok", "形象已就绪"));
  });

  /* ---------- 播放 ---------- */
  function openMedia() {
    const MS = mediaSource();
    if (!MS) { status(t("avatar.no_mse", "这个浏览器不支持实时视频，换 Chrome 或新版 Safari"), true); return false; }
    st.ms = new MS();
    const v = $("#avVideo");
    v.src = URL.createObjectURL(st.ms);
    st.ms.addEventListener("sourceopen", () => {
      // mode="sequence": 我们灌的是一段段独立的 fMP4, 让浏览器按到达顺序接续,
      // 不去解读各段自己的时间戳 (那些段之间本来就不连续)。
      st.sb = st.ms.addSourceBuffer(RT_CODEC);
      st.sb.mode = "sequence";
      st.sb.addEventListener("updateend", pump);
    }, { once: true });
    return true;
  }

  function pump() {
    if (!st.sb || st.sb.updating || !st.queue.length) return;
    try { st.sb.appendBuffer(st.queue.shift()); } catch { /* 缓冲满, 下一轮再来 */ }
    // 缓冲无限长会吃内存; 播过 30s 就裁掉前面的。
    const v = $("#avVideo");
    if (v.currentTime > 40 && st.sb.buffered.length &&
        v.currentTime - st.sb.buffered.start(0) > 30) {
      try { st.sb.remove(0, v.currentTime - 10); } catch { /* 忽略 */ }
    }
  }

  /* ---------- 通话 ---------- */
  async function startCall() {
    if (st.ws) return stopCall();
    if (!openMedia()) return;
    $("#avHint").style.display = "none";
    $("#avCall").textContent = t("avatar.hangup", "挂断");

    const person = $("#avPerson").value;
    const voice = $("#avVoice").value;
    const q = new URLSearchParams({ token: st.sess.token });
    if (person) q.set("person", person);
    if (voice) q.set("voice", voice);
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/avatar/ws?${q}`);
    ws.binaryType = "arraybuffer";
    st.ws = ws;

    ws.onmessage = (e) => {
      if (typeof e.data !== "string") {
        st.queue.push(new Uint8Array(e.data));
        pump();
        const v = $("#avVideo");
        if (v.paused) v.play().catch(() => { /* 需要用户手势, 已经点过了 */ });
        if (!st.t0) startTimer();
        return;
      }
      const m = JSON.parse(e.data);
      // 排队时**一定要说话** — 一声不吭地等, 用户只会以为卡死了然后反复重连,
      // 而每次重连都要重新排。
      if (m.type === "queued") {
        status(m.ahead > 0
          ? t("avatar.queued_n", `排队中，前面还有 ${m.ahead} 位…`)
          : t("avatar.queued_next", "排队中，马上轮到你…"));
      } else if (m.type === "busy") {
        status(t("avatar.busy", "通道占线，稍后再试"), true); stopCall();
      } else if (m.type === "error") {
        status(m.message || t("avatar.error", "出错了"), true);
      }
    };
    ws.onclose = () => stopCall();
    ws.onerror = () => status(t("avatar.error", "连接失败"), true);
  }

  /* 计时与花费: 从**第一帧视频**起算 — 与服务端的计费口径一致, 排队不计。
     两边口径不一样的话, 用户看到的数字和账单对不上, 那比不显示更糟。 */
  function startTimer() {
    st.t0 = Date.now();
    st.timer = setInterval(() => {
      const s = Math.floor((Date.now() - st.t0) / 1000);
      $("#avTimer").textContent =
        String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
      // 服务端向上取整到分钟, 这里同口径
      const mins = Math.floor(s / 60) + (s % 60 ? 1 : 0);
      $("#avCost").textContent = mins ? `−${mins * st.rate}` : "";
    }, 1000);
  }

  function stopCall() {
    if (st.ws) { try { st.ws.close(); } catch { /* 忽略 */ } st.ws = null; }
    if (st.ear) { st.ear.stop(); st.ear = null; }
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
    st.t0 = null; st.queue = [];
    $("#avCall").textContent = t("avatar.start", "开始通话");
    $("#avHint").style.display = "";
    boot();                              // 刷新余额
  }

  $("#avCall").addEventListener("click", startCall);
  boot();
})();
