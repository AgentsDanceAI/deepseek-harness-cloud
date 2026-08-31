/* DSH Cloud 智能体工作台前端。
 *
 * 刻意没有构建步骤: 一个 HTML + 一个 CSS + 这个文件, 改完刷新就见效。这个界面
 * 的复杂度撑不起一套打包工具链, 而每多一层构建就多一处"本地好好的、镜像里坏了"。
 */
const $ = (s) => document.querySelector(s);
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

const state = { sid: null, ws: null, cli: "claude", clis: [], turnCredits: 0, busy: false };

/* ---------- 通用 ---------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

/* ---------- 积分 ---------- */
async function refreshCredits() {
  const d = await api("/api/credits");
  if (!d.available) { $("#cBalance").textContent = "—"; $("#cPlan").textContent = d.reason || "读不到"; return; }
  $("#cBalance").textContent = d.balance ?? "—";
  $("#cPlan").textContent = d.plan || "";
  $("#cMinutes").textContent = d.minutes_left != null ? `剩 ${d.minutes_left} 分钟` : "";
  $("#sideFoot").textContent = `${d.credits_per_min ?? "?"} 积分/分钟 · 闲置 ${d.idle_stop_min ?? "?"} 分钟回收`;
}

/* ---------- 会话 ---------- */
async function loadSessions() {
  const { sessions } = await api("/api/sessions");
  const nav = $("#sessions"); nav.innerHTML = "";
  for (const s of sessions) {
    const row = el("div", "sess" + (s.id === state.sid ? " on" : ""));
    row.appendChild(el("div", "sess-title", s.title));
    const del = el("button", "sess-del", "×");
    del.onclick = async (e) => {
      e.stopPropagation();
      await api(`/api/sessions/${s.id}`, { method: "DELETE" });
      if (state.sid === s.id) { state.sid = null; $("#log").innerHTML = ""; }
      loadSessions();
    };
    row.appendChild(del);
    row.onclick = () => openSession(s.id);
    nav.appendChild(row);
  }
  if (!state.sid && sessions.length) openSession(sessions[0].id);
}

async function newSession() {
  const s = await api("/api/sessions", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ cli: state.cli }),
  });
  await loadSessions();
  openSession(s.id);
}

async function openSession(sid) {
  state.sid = sid;
  state.turnCredits = 0; $("#cTurn").textContent = "0";
  document.querySelectorAll(".sess").forEach((n) => n.classList.remove("on"));
  const { messages } = await api(`/api/sessions/${sid}/messages`);
  const log = $("#log"); log.innerHTML = "";
  for (const m of messages) addMessage(m.role, m.text);
  loadSessions();
  connectChat();
}

/* ---------- 消息渲染 ---------- */
function addMessage(role, text) {
  const wrap = el("div", "msg " + role);
  wrap.appendChild(el("div", "who", role === "user" ? "你" : state.cliName || "助手"));
  const body = el("div", "body");
  body.innerHTML = role === "user" ? escapeHtml(text) : render(text);
  wrap.appendChild(body);
  $("#log").appendChild(wrap);
  scrollLog();
  return body;
}
function escapeHtml(s) { const d = el("div"); d.textContent = s; return d.innerHTML; }
function render(md) {
  // marked 没加载成功时退回纯文本 —— 宁可样式丑, 不要整条消息变成空白。
  try { return window.marked ? window.marked.parse(md) : escapeHtml(md); }
  catch { return escapeHtml(md); }
}
function scrollLog() { const l = $("#log"); l.scrollTop = l.scrollHeight; }

/* ---------- 对话 WebSocket ---------- */
function connectChat() {
  if (state.ws) { state.ws.onclose = null; state.ws.close(); }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/chat/${state.sid}`);
  state.ws = ws;
  let bodyEl = null, buf = "", tools = {};

  ws.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.t === "user_echo") { /* 已在发送时本地渲染 */ return; }
    if (ev.t === "delta" || ev.t === "text") {
      if (!bodyEl) { bodyEl = addMessage("assistant", ""); bodyEl.classList.add("cursor"); }
      buf += ev.text;
      bodyEl.innerHTML = render(buf);
      scrollLog();
    } else if (ev.t === "thinking") {
      if (!bodyEl) bodyEl = addMessage("assistant", "");
      let t = bodyEl.querySelector(".think");
      if (!t) { t = el("div", "think"); bodyEl.appendChild(t); }
      t.textContent += ev.text;
      scrollLog();
    } else if (ev.t === "tool") {
      if (!bodyEl) bodyEl = addMessage("assistant", "");
      const d = el("details", "tool");
      const sum = el("summary");
      sum.appendChild(el("span", "tname", ev.name || "工具"));
      sum.appendChild(el("span", "muted", "运行中…"));
      d.appendChild(sum);
      const pre = el("pre", null, JSON.stringify(ev.input || {}, null, 1).slice(0, 4000));
      d.appendChild(pre);
      bodyEl.appendChild(d);
      tools[ev.id] = { d, sum };
      scrollLog();
    } else if (ev.t === "tool_end") {
      const t = tools[ev.id];
      if (t) {
        t.sum.lastChild.textContent = ev.ok ? "完成" : "失败";
        t.sum.lastChild.className = ev.ok ? "ok" : "bad";
        if (ev.output) t.d.appendChild(el("pre", null, String(ev.output).slice(0, 4000)));
      }
    } else if (ev.t === "done") {
      const u = ev.usage || {};
      $("#cTurn").textContent = `${u.input || 0}↑ ${u.output || 0}↓`;
      refreshCredits();
    } else if (ev.t === "error") {
      $("#log").appendChild(Object.assign(el("div", "msg"), { innerHTML: `<div class="err">${escapeHtml(ev.message)}</div>` }));
      scrollLog();
    } else if (ev.t === "raw") {
      console.debug("[raw]", ev.line);
    } else if (ev.t === "turn_end") {
      if (bodyEl) bodyEl.classList.remove("cursor");
      bodyEl = null; buf = ""; tools = {};
      setBusy(false);
      loadSessions();
    }
  };
  ws.onclose = () => { setBusy(false); };
}

function setBusy(b) {
  state.busy = b;
  $("#sendBtn").disabled = b;
  $("#stopBtn").hidden = !b;
}

/* ---------- 发送 ---------- */
$("#composer").onsubmit = (e) => {
  e.preventDefault();
  const t = $("#input").value.trim();
  if (!t || state.busy || !state.ws || state.ws.readyState !== 1) return;
  addMessage("user", t);
  state.ws.send(JSON.stringify({ t: "send", text: t }));
  $("#input").value = ""; $("#input").style.height = "auto";
  setBusy(true);
};
$("#stopBtn").onclick = () => state.ws && state.ws.send(JSON.stringify({ t: "stop" }));
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
});
$("#input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
});
$("#newBtn").onclick = newSession;
// 窄屏下侧栏是推出屏幕的 —— 没有这个按钮, 手机上摸不到会话和积分。
$("#menuBtn").onclick = () => $("#side").classList.toggle("open");
$("#sessions").addEventListener("click", () => $("#side").classList.remove("open"));

/* ---------- 标签页 ---------- */
document.querySelectorAll("#tabs button[data-tab]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#tabs button[data-tab]").forEach((x) => x.classList.remove("on"));
    document.querySelectorAll(".pane").forEach((p) => p.classList.remove("on"));
    b.classList.add("on");
    $(`#pane-${b.dataset.tab}`).classList.add("on");
    if (b.dataset.tab === "shell") initShell();
    if (b.dataset.tab === "files") loadTree("");
    if (b.dataset.tab === "git") loadGit();
  };
});

/* ---------- 终端 ---------- */
let term = null, termWs = null;
function initShell() {
  if (term) { term.focus(); return; }
  if (!window.Terminal) { $("#term").textContent = "终端组件没加载成功"; return; }
  term = new window.Terminal({ fontSize: 13, cursorBlink: true, convertEol: true,
                               theme: { background: "#00000000" }, allowTransparency: true });
  term.open($("#term"));
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  termWs = new WebSocket(`${proto}//${location.host}/ws/shell`);
  let firstOut = false;
  termWs.onmessage = (e) => {
    term.write(e.data);
    // 等 shell 真正打出东西再对齐尺寸。连上就 fit 的话, resize 会在 shell
    // 起来之前往 PTY 里写终端查询序列, 而没人读它 —— 那串回显就是屏幕顶上
    // 那行 `vvvv…` 乱码 (实测出来的)。
    if (!firstOut) { firstOut = true; setTimeout(fitTerm, 120); }
  };
  termWs.onopen = () => term.focus();
  term.onData((d) => termWs.readyState === 1 && termWs.send(JSON.stringify({ t: "in", data: d })));
  window.addEventListener("resize", fitTerm);
}
function fitTerm() {
  if (!term || !termWs || termWs.readyState !== 1) return;
  // 没有 fit 插件, 自己按字符宽高算 —— 少一个依赖, 逻辑也就十行。
  const box = $("#term").getBoundingClientRect();
  const cols = Math.max(20, Math.floor((box.width - 16) / 8));
  const rows = Math.max(6, Math.floor((box.height - 16) / 17));
  term.resize(cols, rows);
  termWs.send(JSON.stringify({ t: "resize", cols, rows }));
}

/* ---------- 文件 ---------- */
async function loadTree(path) {
  const d = await api(`/api/files?path=${encodeURIComponent(path)}`);
  const box = $("#fileTree");
  if (!path) box.innerHTML = "";
  for (const e of d.entries || []) {
    const n = el("div", "node");
    n.innerHTML = `<span class="ic">${e.dir ? "▸" : "·"}</span>${escapeHtml(e.name)}`;
    n.style.paddingLeft = 8 + (e.path.split("/").length - 1) * 12 + "px";
    n.onclick = () => e.dir ? toggleDir(n, e) : openFile(e.path, n);
    box.appendChild(n);
  }
}
async function toggleDir(node, entry) {
  if (node.dataset.open) {
    let s = node.nextSibling;
    while (s && s.dataset && s.dataset.parent === entry.path) { const nx = s.nextSibling; s.remove(); s = nx; }
    delete node.dataset.open; node.querySelector(".ic").textContent = "▸";
    return;
  }
  const d = await api(`/api/files?path=${encodeURIComponent(entry.path)}`);
  let anchor = node;
  for (const e of d.entries || []) {
    const n = el("div", "node");
    n.innerHTML = `<span class="ic">${e.dir ? "▸" : "·"}</span>${escapeHtml(e.name)}`;
    n.style.paddingLeft = 8 + e.path.split("/").length * 12 + "px";
    n.dataset.parent = entry.path;
    n.onclick = () => e.dir ? toggleDir(n, e) : openFile(e.path, n);
    anchor.after(n); anchor = n;
  }
  node.dataset.open = "1"; node.querySelector(".ic").textContent = "▾";
}
async function openFile(path, node) {
  document.querySelectorAll("#fileTree .node").forEach((n) => n.classList.remove("on"));
  if (node) node.classList.add("on");
  const d = await api(`/api/file?path=${encodeURIComponent(path)}`);
  const view = $("#fileView"); view.innerHTML = "";
  if (d.error) { view.appendChild(el("div", "empty", d.error)); return; }
  const ta = el("textarea"); ta.value = d.text;
  const save = el("button", null, "保存");
  save.onclick = async () => {
    const r = await api("/api/file", { method: "PUT", headers: { "content-type": "application/json" },
                                       body: JSON.stringify({ path, text: ta.value }) });
    save.textContent = r.ok ? "已保存" : (r.error || "失败");
    setTimeout(() => (save.textContent = "保存"), 1600);
  };
  const bar = el("div"); bar.style.cssText = "display:flex;gap:8px;align-items:center;margin-bottom:8px";
  bar.appendChild(el("b", null, path)); bar.appendChild(save);
  view.appendChild(bar); view.appendChild(ta);
}

/* ---------- 版本 ---------- */
async function loadGit() {
  const d = await api("/api/git/status");
  $("#gitBranch").textContent = d.repo ? `分支 ${d.branch}` : "这不是一个 git 仓库";
  const box = $("#gitFiles"); box.innerHTML = "";
  for (const f of d.files || []) {
    const n = el("div", "gitfile");
    n.appendChild(el("span", "st", f.state));
    n.appendChild(el("span", null, f.path));
    n.onclick = async () => {
      const r = await api(`/api/git/diff?path=${encodeURIComponent(f.path)}`);
      const pre = $("#gitDiff"); pre.className = ""; pre.innerHTML = "";
      for (const line of (r.diff || "(无 diff)").split("\n")) {
        const cls = line.startsWith("+") && !line.startsWith("+++") ? "diff-add"
                  : line.startsWith("-") && !line.startsWith("---") ? "diff-del" : "";
        pre.appendChild(el("div", cls, line));
      }
    };
    box.appendChild(n);
  }
  if (!(d.files || []).length && d.repo) box.appendChild(el("div", "empty", "没有改动"));
}
$("#commitForm").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("#commitMsg").value.trim();
  if (!msg) return;
  const r = await api("/api/git/commit", { method: "POST", headers: { "content-type": "application/json" },
                                           body: JSON.stringify({ message: msg }) });
  $("#gitDiff").className = ""; $("#gitDiff").textContent = r.error || r.output || "已提交";
  $("#commitMsg").value = "";
  loadGit();
};

/* ---------- 启动 ---------- */
(async function boot() {
  const cfg = await api("/api/config");
  state.cli = cfg.cli; state.clis = cfg.clis || [];
  state.cliName = (state.clis.find((c) => c.id === cfg.cli) || {}).name || cfg.cli;
  $("#cliName").textContent = state.cliName;
  const pick = $("#cliPick");
  pick.innerHTML = "";
  for (const c of state.clis) {
    const o = el("option", null, c.name); o.value = c.id; pick.appendChild(o);
  }
  pick.value = state.cli;
  pick.hidden = state.clis.length < 2;
  pick.onchange = () => { state.cli = pick.value; newSession(); };

  await refreshCredits();
  // 每分钟刷一次余额 —— 按分钟计费, 用户该能看着它变。
  setInterval(refreshCredits, 60000);
  await loadSessions();
  if (!state.sid) await newSession();
})();
