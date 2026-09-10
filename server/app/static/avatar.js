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
  /* **成套的预设**: 一个人 = 一张脸 + 一副嗓子, 绑死, 不给自由组合。
     老板 2026-09-01 定的, 起因是他选了男形象配上女嗓音, 出来一个女头贴在男身上。
     那不是配错了参数, 是这个产品本来就不该让人配 —— 样子、声音、静止图、说话时
     的画面, 四样必须是同一个人。

     键是形象库里的文件名; 名字与嗓音是我们这边的事 (上游只知道 id)。加人要在
     这里加一行, 这是故意的: 选单里只放校对过的。 */
  const PRESETS = {
    "source-v3-head": { name: t("js.avatar.p.default", "初雪 · 温柔"), voice: "xiaoya" },
    "lin": { name: t("js.avatar.p.lin", "林 · 安静"), voice: "xiaoxiao" },
    "yue": { name: t("js.avatar.p.yue", "悦 · 干练"), voice: "hsiaochen" },
    "chen": { name: t("js.avatar.p.chen", "晨 · 沉稳"), voice: "yunjian" },
    "hao": { name: t("js.avatar.p.hao", "皓 · 阳光"), voice: "yunxi" },
  };
  /* 半双工 / 全双工。
     全双工 = 她说话时麦克风照开, 你一出声就把她打断。安静环境里这是最像打电话
     的那种体验, 但**嘈杂环境里会乱成一团**: 音箱里她自己的声音被麦克风听回去,
     于是她把自己打断, 一轮接一轮; 旁边有人聊天同理。
     半双工 = 她说话时闭麦, 说完再听。插不了嘴, 但不会自己打断自己。
     默认半双工: 插不了嘴只是不够灵活, 自己打断自己看起来是产品坏了。 */
  const DUPLEX_KEY = "dhc.avatar.duplex";
  function loadDuplex() {
    try { return localStorage.getItem(DUPLEX_KEY) === "full" ? "full" : "half"; }
    catch { return "half"; }             // 无痕窗口里读 localStorage 会抛
  }
  const st = {
    sess: null, cfg: null, ws: null, ear: null, history: [], sid: 0,
    ms: null, sb: null, url: null, queue: [], speaking: false, watch: null,
    t0: null, timer: null, rate: 0,
    duplex: loadDuplex(), micOff: false,
    // 她最近说过的几句 —— 用来认出从音箱绕回来的她自己, 见 isEcho。
    herSaid: [], micTimer: null,
  };

  /* iPhone 没有标准 MediaSource — iOS 17.1+ 给的是同形的 ManagedMediaSource。
     两个都没有就只能退纯语音 (这页的意义就没了, 所以直接说清楚)。 */
  function mediaSource() {
    const w = window;
    if (w.MediaSource?.isTypeSupported?.(RT_CODEC)) return w.MediaSource;
    if (w.ManagedMediaSource?.isTypeSupported?.(RT_CODEC)) return w.ManagedMediaSource;
    return null;
  }

  /* 对话字幕落在**画面上**, 和她在一起 —— 挤在右栏的状态行里, 眼睛要在屏幕两头
     来回跑, 而这本来就是一通电话, 字幕就该在脸下面。 */
  function say2log(who, text) {
    const box = $("#avLog");
    const line = document.createElement("div");
    line.className = "av-line av-" + who;
    line.textContent = text;
    box.appendChild(line);
    while (box.children.length > 30) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
    box.hidden = false;
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
      status(t("js.avatar.no_credits", "积分不足，通话需要 {n} 积分/分钟")
               .replace("{n}", s.d.credits_per_min), true);
      $("#avCall").disabled = true;
      return;
    }
    if (!s.ok) { status(t("js.avatar.unavailable", "数字人暂时不可用"), true); return; }
    st.sess = s.d;
    st.rate = s.d.credits_per_min;
    $("#avBalance").textContent = s.d.balance;
    $("#avRate").textContent = `${s.d.credits_per_min} ${t("js.avatar.per_min", "积分/分钟")}`;

    // 形象与音色清单由 GPU 侧给, 且**已按租户过滤** — 别人上传的脸不会在这里。
    const c = await api(`/api/avatar/config`);
    if (!c.ok) { status(t("js.avatar.unavailable", "数字人暂时不可用"), true); return; }
    st.cfg = c.d;
    // 只列我们做好的那几套。上游库里可能还留着别的 (口袋专家那条线在用), 但
    // 没配成套的不该出现在这里。
    const known = Object.keys(PRESETS).filter(p => (c.d.persons || []).includes(p));
    fill($("#avPerson"), known, c.d.person_default,
         Object.fromEntries(known.map(p => [p, PRESETS[p].name])));
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

  /* 重建选项时**保住已经选好的那个人**。
     boot() 不只在开页时跑 —— 挂断电话后也会跑一次 (刷新余额), 而 innerHTML=""
     会把选择冲回第一项"默认"。表现是: 换成林、打一通、挂断, 下一通又变回初雪,
     而侧栏里那行小字变了没人会注意 —— 看到的就是"我明明换了人, 脸还是之前的"。 */
  function fill(sel, ids, def, names) {
    const keep = sel.value;
    sel.innerHTML = "";
    const o0 = document.createElement("option");
    o0.value = ""; o0.textContent = t("js.avatar.default", "默认") + (def ? `（${def}）` : "");
    sel.appendChild(o0);
    for (const id of ids) {
      const o = document.createElement("option");
      o.value = id; o.textContent = (names && names[id]) || id;
      sel.appendChild(o);
    }
    // 选过的人还在清单里就选回去; 不在了 (上游下架) 就落回默认, 而不是留一个
    // 选不中的值 —— 那会让 value 与显示出来的项对不上。
    if (keep && Array.prototype.some.call(sel.options, (o) => o.value === keep)) sel.value = keep;
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
  $("#avSay").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const said = e.target.value.trim();
    e.target.value = "";
    if (said) reply(said);
  });

  // 换人要**同时**换背景和重算视频层位置 —— 只换一个就是错位。
  $("#avPerson").addEventListener("change", () => { loadBg(); layout(); });

  /* ---------- 上传形象 ---------- */
  /* ---------- 播放 ---------- */
  function openMedia() {
    const MS = mediaSource();
    if (!MS) { status(t("js.avatar.no_mse", "这个浏览器不支持实时视频，换 Chrome 或新版 Safari"), true); return false; }
    const v = $("#avVideo");
    // ManagedMediaSource (Safari) 硬性要求关掉远程投播, 否则 addSourceBuffer 直接抛。
    if (!window.MediaSource?.isTypeSupported?.(RT_CODEC)) v.disableRemotePlayback = true;
    st.ms = new MS();
    st.url = URL.createObjectURL(st.ms);
    v.src = st.url;
    st.ms.addEventListener("sourceopen", () => {
      // mode="sequence": 我们灌的是一段段独立的 fMP4, 让浏览器按到达顺序接续,
      // 不去解读各段自己的时间戳 (那些段之间本来就不连续)。
      st.sb = st.ms.addSourceBuffer(RT_CODEC);
      st.sb.mode = "sequence";
      // ⚠️ 直播流**必须**显式 duration=Infinity。不设的话 MediaSource 的 duration
      // 等于已缓冲的末尾, 播到那儿浏览器就判定"流结束"派发 ended, 此后新到的分片
      // 只进缓冲不再播 —— 表现是字节在收、计时在走、账也在扣, 画面一帧不动。
      // 她说话是不定长的续写流, 语义上本就没有 duration。
      try { st.ms.duration = Infinity; } catch { /* 老实现不认 */ }
      st.sb.addEventListener("updateend", pump);
      st.sb.addEventListener("error", () => console.error("[avatar] SourceBuffer 出错"));
      console.info("[avatar] sourceopen, SourceBuffer 已建");
      pump();
    }, { once: true });
    // 在**点击这一跳里**起播: 自动播放策略认的是用户手势, 等到第一帧再 play
    // 就晚了 (而失败是静默的)。
    v.play().catch((e) => console.info("[avatar] 首次 play 被拒 (到货后再试):", e.name));
    v.addEventListener("error", () => console.error("[avatar] video 元素报错:", v.error?.code));
    // **她说完没说完, 看画面动没动** —— 不看事件也不看上游的文字队列:
    //   · 上游的 idle 只是"没有待念的文字了", 那时缓冲里还有好几秒在播, 拿它藏
    //     图层她会在话说一半时消失;
    //   · waiting/ended 这类事件在缓冲耗尽时**不一定发**, 而漏一次的后果是最后
    //     一帧僵在背景上、与静止图错开半分 —— 就是老板说的"重影, 好吓人"。
    // 直接比 currentTime: 连着两拍没往前走就是停了, 这个判据不依赖任何事件。
    clearInterval(st.watch);
    let last = -1;
    st.watch = setInterval(() => {
      const now = v.currentTime;
      if (now !== last) { last = now; showVideo(true); return; }
      if (st.speaking) showVideo(false);      // 画面不动了 = 她说完了
    }, 200);
    return true;
  }

  /* 打断她 = **整管重建 MSE**, 不只是清队列: 半路截断的 fMP4 会把 SourceBuffer
     弄进错误态, 之后灌什么都不播。重建的同时画面回到静止背景 —— 那正是"她闭嘴"
     该有的样子 (静止图就是模型的中性帧, 与视频同源)。 */
  function rebuildMedia() {
    const v = $("#avVideo");
    if (st.url) { try { URL.revokeObjectURL(st.url); } catch { /* 忽略 */ } st.url = null; }
    st.sb = null; st.ms = null; st.queue = [];
    try { v.pause(); v.removeAttribute("src"); v.load(); } catch { /* 忽略 */ }
    showVideo(false);
    return openMedia();
  }

  /* 她不说话时露静止背景, 说话时才盖上视频层。不切的话最后一帧会僵在那儿。
     这里也是 speaking 翻转的**唯一**入口, 所以半双工的闸就挂在这条路上 ——
     挂在别处早晚会漏掉一条翻转路径。 */
  function showVideo(on) {
    if (on === st.speaking) return;
    st.speaking = on;
    if (on) paintVideo(); else $("#avVideo").style.opacity = "0";
    if (st.duplex === "half") micGate(!on);
  }

  /* 露出视频层, 但**必须等它真的解出了一帧、且盒子已经按当前形象排好**。
   *
   * 之前是 currentTime 一动就把 opacity 拉到 1。iOS 上抓到过后果 (录屏第 82 帧):
   * 那一瞬视频元素还没套上 CSS 的百分比盒子, 整张脸以原始尺寸糊在画面上 ——
   * 一个放大两倍多、右边和上边都是硬边的方块。只闪一帧, 所以平均值、场景检测
   * 都看不出来, 得逐帧翻才找得到。
   *
   * 三道: 等 videoWidth (有第一帧了) + readyState≥2 (能画了), 重算一次盒子,
   * 再等一个动画帧让排版落地, 才把它显出来。
   * 兜底 30 帧 (~0.5s) 后无论如何显出来 —— 宁可闪一下, 也不能永远不出画。
   */
  function paintVideo() {
    const v = $("#avVideo");
    let tries = 0;
    (function tick() {
      if (!st.speaking) return;               // 等的这会儿她说完了
      if (v.videoWidth && v.readyState >= 2) {
        layout();                             // 盒子按当前形象重算 (幂等, 便宜)
        raf(() => { if (st.speaking) v.style.opacity = "1"; });
        return;
      }
      if (++tries > 30) { v.style.opacity = "1"; return; }
      raf(tick);
    })();
  }

  /* 没有 rAF 的环境 (测试桩、老浏览器) 退回定时器 —— 少一帧延迟, 不影响判定。 */
  function raf(fn) {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(fn);
    else setTimeout(fn, 16);
  }

  /* ---------- 回声: 别把她自己听回去 ---------- */
  /* 半双工靠的是**时序**闸(她说话时停掉识别器), 而时序闸有两个躲不掉的漏点:
   *
   *   1. **音箱里的声音比 currentTime 慢。** 我们判定"她说完了"看的是画面停没停,
   *      而那一刻声音还在往外走 —— 设备输出缓冲几十毫秒, 蓝牙音箱/耳机是 100~300
   *      毫秒, 再加上房间的混响尾巴。闸一开就正好收到她最后半个字。
   *   2. **中途卡一下也会被当成"她说完了"。** 判据是 currentTime 连着两拍没动,
   *      而网络抖一下、缓冲见底同样不动。于是闸在她话说到一半时打开, 她接着说,
   *      这时 st.speaking 是 false —— onresult 里那道 `speaking` 判断根本拦不住。
   *
   * 所以再加一道**按内容**的闸: 她说的每一句我们都有原文(是我们发给上游让她念的),
   * 听回来的如果就是那几句里的一段, 那必然是绕回来的, 不是人说的。
   * 这道闸与时序无关, 上面两个漏点它都盖得住; **全双工更需要它** —— 那个模式下
   * 麦克风全程开着, 时序闸压根不存在。
   *
   * 判据故意保守, 宁可漏也不要误伤真人:
   *   · 少于 4 个字不算 —— "好的""对"这种谁都会说, 拿它当回声会把人憋死;
   *   · 只比她 20 秒内说过的 —— 再早的声音不可能还在空气里;
   *   · 双向包含 —— 识别器可能只听清一半("看清楚再说"), 也可能连着两句一起吐。
   */
  const ECHO_WINDOW_MS = 20000;
  const ECHO_MIN_CHARS = 4;

  /* 比对前把标点、空白、大小写抹平: 识别器给的标点和我们发下去的从来对不上。 */
  function normSaid(x) {
    return String(x || "").toLowerCase().replace(/[\s\p{P}\p{S}]/gu, "");
  }

  /* 她要念的每一句都登记一下。**登记点是"发给上游"那一刻**, 不是"播出来"那一刻
     —— 后者散落在好几条路上(首句问候、逐句回答), 漏登记一条就漏一条回声。 */
  function herSpoke(text) {
    const v = normSaid(text);
    if (!v) return;
    st.herSaid.push({ s: v, at: Date.now() });
    while (st.herSaid.length > 6) st.herSaid.shift();
  }

  function isEcho(said) {
    const v = normSaid(said);
    if (v.length < ECHO_MIN_CHARS) return false;
    const now = Date.now();
    return st.herSaid.some((h) =>
      now - h.at < ECHO_WINDOW_MS && (h.s.includes(v) || v.includes(h.s)));
  }

  /* 半双工的闸: 她说话时把识别器停掉, 说完再开。
     只靠 onresult 里判断是不够的 —— 识别器照样在听, 而 stop() 时会把这期间听
     到的东西定稿吐出来, 那正是从音箱里绕回来的她自己。所以要真的停。

     ⚠️ 开麦要**等一下**再开: 见上面回声那段第 1 条, 画面停住的那一刻声音还在往外
     走。等 MIC_REOPEN_MS 再开, 把设备输出缓冲和混响尾巴让过去。这段等待对真人
     没有代价 —— 没有人能在她话音落下半秒内就接上话。 */
  const MIC_REOPEN_MS = 500;

  function micGate(on) {
    const ear = st.ear;
    st.micOff = !on;
    clearTimeout(st.micTimer);
    st.micTimer = null;
    if (!ear || !st.ws) return;
    if (on) {
      st.micTimer = setTimeout(() => {
        st.micTimer = null;
        // 等的这会儿她可能又开口了, 或者电话已经挂了。
        if (st.micOff || st.ear !== ear || !st.ws) return;
        try { ear.start(); } catch { /* 已在跑 */ }
      }, MIC_REOPEN_MS);
      turnHint(t("js.avatar.listening", "说话吧，她在听"));
    } else {
      try { ear.stop(); } catch { /* 已停 */ }
      turnHint(t("js.avatar.her_turn", "她在说…（说完再开口）"));
    }
  }

  /* 轮到谁说话的提示。**不覆盖错误**: 这条每轮都写一次, 而"麦克风被拒绝了"
     那类消息只写一次 —— 不挡住的话, 用户看到的是一句每轮都在变的正常提示,
     而真正要看的那句一闪就没了。 */
  function turnHint(msg) {
    if ($("#avStatus").classList.contains("av-bad")) return;
    status(msg);
  }

  /* 切换双工方式。通话中也能切 —— 走进嘈杂的地方正是要切的时候。 */
  function setDuplex(mode) {
    st.duplex = mode === "full" ? "full" : "half";
    try { localStorage.setItem(DUPLEX_KEY, st.duplex); } catch { /* 无痕窗口 */ }
    if (!st.ws) return;
    if (st.duplex === "full") micGate(true);   // 全双工: 无论她说不说, 一直听
    else micGate(!st.speaking);
  }

  function pump() {
    if (!st.sb || st.sb.updating || !st.queue.length) return;
    try {
      st.sb.appendBuffer(st.queue.shift());
    } catch (e) {
      // **不能静默**: QuotaExceeded 是"缓冲满了, 下一轮再来", 而其它错 (多半是
      // 流与 codec 对不上, 或 SourceBuffer 已进错误态) 意味着这通电话再也不会
      // 出画 —— 而两者从外面看一模一样, 都是"画面不动"。
      if (e.name === "QuotaExceededError") return;
      console.error("[avatar] appendBuffer 失败:", e.name, e.message);
      status(t("js.avatar.play_failed", "画面播不出来"), true);
    }
    // 缓冲无限长会吃内存; 播过 30s 就裁掉前面的。
    const v = $("#avVideo");
    if (v.currentTime > 40 && st.sb.buffered.length &&
        v.currentTime - st.sb.buffered.start(0) > 30) {
      try { st.sb.remove(0, v.currentTime - 10); } catch { /* 忽略 */ }
    }
  }

  /* ---------- 通话 ---------- */
  /* 通话中锁住换人。**这不是省事, 是必须**: 形象与嗓音是随 WebSocket 建立时的
     查询串定下的, 中途改只能换掉背景图, 说话的还是接通时那个人 —— 于是女头贴在
     男身上 (2026-09-01 老板撞到的正是这个)。想换人就挂断再打。 */
  function lockPicker(on) { $("#avPerson").disabled = on; }

  async function startCall() {
    if (st.ws) return stopCall();
    if (!openMedia()) return;
    $("#avHint").style.display = "none";
    $("#avCall").textContent = t("js.avatar.hangup", "挂断");

    const person = $("#avPerson").value;
    const voice = (PRESETS[person] || {}).voice || "";   // 嗓音跟着人走, 不单选
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
        showVideo(true);                     // 又有话了
        const v = $("#avVideo");
        if (v.paused) v.play().catch(() => { /* 起播被拒, 下一块再试 */ });
        if (!st.t0) startTimer();
        return;
      }
      const m = JSON.parse(e.data);
      // 排队时**一定要说话** — 一声不吭地等, 用户只会以为卡死了然后反复重连,
      // 而每次重连都要重新排。
      if (m.type === "queued") {
        status(m.ahead > 0
          ? t("js.avatar.queued_n", "排队中，前面还有 {n} 位…").replace("{n}", m.ahead)
          : t("js.avatar.queued_next", "排队中，马上轮到你…"));
      } else if (m.type === "busy") {
        status(t("js.avatar.busy", "通道占线，稍后再试"), true); stopCall();
      } else if (m.type === "error") {
        status(m.message || t("js.avatar.error", "出错了"), true);
      } else if (m.type === "ready") {
        // 上游接通了才开始听 —— 早于这一刻识别出来的话没地方发。
        status(t("js.avatar.listening", "说话吧，她在听"));
        listen();
        // **她先开口**。固定一句, 不走模型: 立刻就能说 (模型要好几秒), 而接通后
        // 双方干等的那几秒, 用户只会以为点了没反应。
        const hi = t("js.avatar.hello", "喂，我在呢，你说。");
        say2log("her", hi);
        herSpoke(hi);
        ws.send(JSON.stringify({ type: "say", sid: ++st.sid, text: hi }));
      }
    };
    ws.onclose = () => stopCall();
    ws.onerror = () => status(t("js.avatar.connect_failed", "连接失败"), true);
  }

  /* ---------- 听 ---------- */
  /* 识别用浏览器自带的 SpeechRecognition: 它自带停顿断句, 而这正是电话里最难
     做对的一件事 (自己按静音时长切, 要么把人话腰斩, 要么等到尴尬)。
     代价是 Firefox 没有 —— 那种情况下明说, 别让人对着屏幕干等。 */
  function listen() {
    $("#avSay").hidden = false;              // 打字这条路通话期间一直开着
    lockPicker(true);
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) { status(t("js.avatar.no_asr", "这个浏览器没有语音识别 — 打字也可以"), true); return; }
    const ear = new SR();
    ear.lang = document.documentElement.lang === "en" ? "en-US" : "zh-CN";
    ear.continuous = true;
    ear.interimResults = false;
    ear.onresult = (e) => {
      const said = e.results[e.results.length - 1][0].transcript.trim();
      if (!said) return;
      // 半双工里她还在说 = 这句多半是从音箱绕回来的她自己 (stop() 会把停之前
      // 听到的定稿吐出来)。丢掉, 别让她跟自己对话。
      if (st.duplex === "half" && st.speaking) return;
      // 时序闸拦不住的那部分交给内容闸 (见 isEcho 上面那段)。
      if (isEcho(said)) { console.info("[avatar] 丢掉绕回来的回声:", said); return; }
      reply(said);
    };
    // 识别器会自己停 (静默久了、或一次会话到点)。通话还在就重开 —— 不重开的
    // 表现是"聊着聊着她突然不理人了", 而页面上什么都没变。
    // **但半双工闭麦期间不能重开**, 否则闸刚关上就被这里顶开了。
    ear.onend = () => {
      if (st.ws && st.ear === ear && !st.micOff) { try { ear.start(); } catch { /* 已在跑 */ } }
    };
    ear.onerror = (e) => { if (e.error === "not-allowed") status(t("js.avatar.no_mic", "麦克风被拒绝了"), true); };
    st.ear = ear;
    try { ear.start(); } catch { /* 已在跑 */ }
  }

  /* 说出来的一句 -> 回一句。**先打断她**: 用户开口时她还在说的话, 两个人一起
     出声, 而且她说的已经是上一轮的答案了。 */
  async function reply(said) {
    if (!st.ws) return;
    // 她还在说就真的打断: 发 stop 并**重建 MSE** —— 只发 stop 的话半截 fMP4 会
    // 把 SourceBuffer 弄进错误态, 后面一句也播不出来。
    if (st.speaking) {
      try { st.ws.send(JSON.stringify({ type: "stop" })); } catch { /* 连接没了 */ }
      rebuildMedia();
    }
    st.history.push({ role: "user", content: said });
    say2log("me", said);
    // **按句读, 来一句发一句**: 上游出全文要好几秒, 而电话里等整段等于"没反应"。
    const r = await fetch("/api/avatar/say", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text: said, history: st.history.slice(0, -1) }),
    });
    if (!r.ok || !r.body) { status(t("js.avatar.think_failed", "她没想出该说什么"), true); return; }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let tail = "", whole = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      tail += dec.decode(value, { stream: true });
      const lines = tail.split("\n");
      tail = lines.pop();                     // 最后一截可能是半行
      for (const ln of lines) {
        if (!ln.startsWith("data: ")) continue;
        const raw = ln.slice(6).trim();
        if (raw === "[DONE]") continue;
        let d; try { d = JSON.parse(raw); } catch { continue; }
        if (d.error) { status(t("js.avatar.think_failed", "她没想出该说什么"), true); continue; }
        if (!d.text) continue;
        whole += d.text;
        if (!st.ws) return;                   // 说到一半挂断了
        say2log("her", d.text);
        herSpoke(d.text);
        st.ws.send(JSON.stringify({ type: "say", sid: ++st.sid, text: d.text }));
      }
    }
    if (!whole) { status(t("js.avatar.think_failed", "她没想出该说什么"), true); return; }
    st.history.push({ role: "assistant", content: whole });
    if (st.history.length > 16) st.history.splice(0, st.history.length - 16);
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
    $("#avSay").hidden = true;
    lockPicker(false);
    clearInterval(st.watch); st.watch = null;
    showVideo(false);
    if (st.ear) { const e = st.ear; st.ear = null; e.onend = null; try { e.stop(); } catch { /* 已停 */ } }
    st.micOff = false;
    st.history = [];
    st.herSaid = [];
    clearTimeout(st.micTimer); st.micTimer = null;
    if (st.timer) { clearInterval(st.timer); st.timer = null; }
    st.t0 = null; st.queue = [];
    $("#avCall").textContent = t("js.avatar.start", "开始通话");
    $("#avHint").style.display = "";
    boot();                              // 刷新余额
  }

  const dup = $("#avDuplex");
  if (dup) {
    dup.value = st.duplex;
    dup.addEventListener("change", () => setDuplex(dup.value));
  }
  $("#avCall").addEventListener("click", startCall);
  boot();
})();
