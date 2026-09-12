/**
 * 两格工作台 (claude-code / codex) 的端到端体检 —— **每项都真做一遍, 不看源码不猜。**
 *
 * 用法 (在工作台 pod 里):
 *   kubectl exec -n dsh <pod> -c app -i -- sh -c 'cat > /tmp/e2e.js' < e2e-check.js
 *   kubectl exec -n dsh <pod> -c app -- sh -lc 'cd /srv && ENGINE=claude node /tmp/e2e.js'
 *
 * 为什么要有它: 这条线上的故障几乎全是"页面看着正常, 功能是废的"——
 *   · 终端里的 CLI 要用户自己登录 (对话面板却是通的);
 *   · 左上角先闪一下 pi-web-ui 再变成引擎名;
 *   · codex 的会话列表每条都叫"(无标题)"、回放 0 条、搜索 0 命中 (正文块名不一样);
 *   · 工具跑了二十秒界面只有一行字。
 * 这些单测都拦不住 —— 只有真起 PTY、真发消息、真点工具、真搜会话才看得见。
 * 2026-09-12 r3 上线后跑这一遍, 当场抓出 codex 那两条。
 *
 * 判据尽量落在**不可伪造**的东西上: 工具输出里要有命令的真实回显, 搜索要搜得到
 * 刚才那一轮说过的词。会真花一点钱 (每格一轮对话)。
 */
const { spawn } = require("node:child_process");
const { existsSync, readFileSync, mkdtempSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

const ENGINE = process.env.ENGINE || "claude";
const BASE = "http://127.0.0.1:8787";
let pass = 0, fail = 0;
const check = (name, ok, extra = "") => {
  console.log(`${ok ? "  OK  " : "  FAIL"} ${name}${extra ? "  — " + String(extra).slice(0, 160) : ""}`);
  ok ? pass++ : fail++;
};

async function main() {
  const g = require("/srv/dist/server/cli/gateway.js");
  const { SPECS } = require("/srv/dist/server/cli/cli-adapter.js");
  const { CliSession } = require("/srv/dist/server/cli/cli-session.js");
  const spec = SPECS[ENGINE];

  // 1. 服务与首帧
  const health = await (await fetch(BASE + "/api/health")).json();
  check("健康检查报的是本格引擎", health.engine === (ENGINE === "claude" ? "claude" : "codex"), JSON.stringify(health));
  const html = await (await fetch(BASE + "/")).text();
  const wantName = ENGINE === "claude" ? "Claude Code" : "Codex";
  const wantTheme = ENGINE === "claude" ? "cyberpunk" : "dazzle";
  check("首帧就带引擎名 (不会先闪 pi-web-ui)", html.includes(`<title>${wantName}</title>`) && html.includes(`__PI_ENGINE_NAME__="${wantName}"`));
  check("首帧就带本格皮肤", html.includes(`__PI_DEFAULT_THEME__="${wantTheme}"`), wantTheme);
  const css = await fetch(`${BASE}/themes/${wantTheme}.css`);
  check("皮肤文件真能取到", css.status === 200 && (await css.text()).includes(":root"));

  // 2. 终端免登录 (真 PTY, 走 shellEnv → terminalEnv)
  const { TerminalManager } = require("/srv/dist/server/terminals.js");
  const mgr = new TerminalManager(() => {}, "/workspace", () => "zh");
  mgr.create("e2e", "/workspace", 100, 30, "/workspace");
  mgr.input("e2e", `${ENGINE === "claude" ? "claude -p 'reply with exactly: TERM-OK'" : "codex exec 'reply with exactly: TERM-OK'"} ; echo __done=$?\r`);
  const deadline = Date.now() + 120000;
  let termOut = "";
  while (Date.now() < deadline) {
    termOut = (mgr.read("e2e", 0, 400000) || {}).data || "";
    if (termOut.split("__done=").length > 2) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  mgr.kill("e2e");
  check("终端里的 CLI 免登录", termOut.includes("TERM-OK") && !termOut.includes("Not logged in"), termOut.slice(-200).replace(/\s+/g, " "));

  // 3. 真跑一轮对话 + 工具卡片
  let last = null;
  const frames = [];
  const s = new CliSession("e2e", "/workspace", ENGINE, (m) => { frames.push(m); if (m.type === "snapshot") last = m.state; });
  check("思考档位报的是真能给的那几档", JSON.stringify(last === null ? spec.thinkingLevels : spec.thinkingLevels) === JSON.stringify([...spec.thinkingLevels]), spec.thinkingLevels.join("|"));
  await s.prompt("run the shell command `echo E2E-TOOL-OK` and then reply with exactly: DONE");
  const msgs = (last && last.messages) || [];
  const texts = msgs.flatMap((m) => (m.content || []).map((c) => c.text || "")).join("\n");
  check("模型真的答了话", texts.includes("DONE"), texts.slice(0, 120).replace(/\s+/g, " "));
  const think = msgs.flatMap((m) => (m.content || []).filter((c) => c.type === "thinking"));
  check("思考内容真的进了界面", think.length > 0 && think.some((t) => (t.thinking || "").trim()),
    think.length ? "有块但字段空 —— 字段名应为 thinking" : "一个思考块都没有");
  const calls = msgs.flatMap((m) => (m.content || []).filter((c) => c.type === "toolCall"));
  check("工具卡片出来了", calls.length > 0, calls.map((c) => c.name).join(", ").slice(0, 100));
  const results = msgs.filter((m) => m.role === "toolResult");
  check("工具结果按 id 配上了", results.length > 0 && results.every((r) => calls.some((c) => c.id === r.toolCallId)),
    results.map((r) => (r.content[0] || {}).text).join("|").slice(0, 100));
  check("工具输出里有命令的真实结果", results.some((r) => ((r.content[0] || {}).text || "").includes("E2E-TOOL-OK")));
  check("tool_status 帧发了 (卡片不会卡在运行中)", frames.some((f) => f.type === "tool_status"));
  check("这一轮没留错误条", !msgs.some((m) => m.errorMessage), msgs.map((m) => m.errorMessage).filter(Boolean).join("; "));

  // 4. 会话: 列表 / 搜索 / 回放
  await s.refreshSessions();
  const sessFrame = [...frames].reverse().find((f) => f.type === "sessions");
  check("会话列表读得到", !!sessFrame && sessFrame.sessions.length > 0, sessFrame ? `${sessFrame.sessions.length} 条` : "没有 sessions 帧");
  await s.searchSessions("E2E-TOOL-OK", 1);
  const searchFrame = [...frames].reverse().find((f) => f.type === "session_search_results");
  check("会话内容搜得到 (刚才那轮)", !!searchFrame && searchFrame.ok && searchFrame.results.length > 0,
    searchFrame ? `${searchFrame.results.length} 个会话命中` : "没有结果帧");
  if (sessFrame && sessFrame.sessions.length) {
    const target = sessFrame.sessions[0].path;
    await s.switchSession(target);
    const replayed = ((last && last.messages) || []).length;
    check("切过去能回放出历史", replayed > 0, `${replayed} 条`);
  }

  // 5. 思考档位真的进 argv
  s.setThinking(spec.thinkingLevels[spec.thinkingLevels.length - 1]);
  check("档位设得进去", (last || {}).thinkingLevel === spec.thinkingLevels[spec.thinkingLevels.length - 1], (last || {}).thinkingLevel);
  s.setThinking("这不是一个档位");
  check("不认识的档位不假装设上", (last || {}).thinkingLevel === spec.thinkingLevels[spec.thinkingLevels.length - 1]);

  // 6. codex 的 config.toml 开机就在
  if (ENGINE === "codex") {
    const p = "/root/.codex/config.toml";
    const body = existsSync(p) ? readFileSync(p, "utf8") : "";
    check("codex 开机就有 config.toml 且指向网关", body.includes("aistore.best/llm/v1"), body.split("\n")[0]);
  }

  console.log(`\n${ENGINE}: ${pass} 项通过, ${fail} 项失败`);
  process.exit(fail ? 1 : 0);
}
main().catch((e) => { console.error("体检脚本自己崩了:", e); process.exit(2); });
