"""把三个 CLI 各自的事件流, 归一成同一套前端事件。

**每个 schema 都是实测抓出来的**, 不是照文档写的 (2026-08-31 逐个跑
`--output-format stream-json` / `--json` 抓的原始输出)。上游改格式时这里会静默
失配 —— 所以每个适配器都留了 `raw` 兜底: 认不出的事件原样透传给前端的调试面板,
而不是丢掉。丢掉的话症状是"发了消息没反应", 查起来毫无线索。

统一事件 (前端只认这几个):
    {"t": "session", "id": ...}          会话 id (用于 --resume)
    {"t": "delta",   "text": ...}        流式正文
    {"t": "text",    "text": ...}        一整段正文 (不支持流式的 CLI 走这个)
    {"t": "thinking","text": ...}        思考内容
    {"t": "tool",    "id","name","input"}工具调用开始
    {"t": "tool_end","id","ok","output"} 工具调用结束
    {"t": "done",    "usage": {...}}     一轮结束
    {"t": "error",   "message": ...}
    {"t": "raw",     "line": ...}        没认出来的原始行
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Adapter:
    """一个 CLI 的接法。"""

    #: 起一轮对话的 argv。resume 为空表示新会话。
    #: prompt 走 stdin 还是 argv 由 stdin_prompt 决定。
    name: str
    exe: str
    stdin_prompt: bool = False
    #: 这一轮的累计用量, 由适配器自己填 (前端拿它换算积分)。
    usage: dict = field(default_factory=dict)

    def argv(self, prompt: str, resume: str | None) -> list[str]:  # pragma: no cover - 被子类覆盖
        raise NotImplementedError

    def stdin_payload(self, prompt: str) -> str | None:
        return None

    def feed(self, line: str) -> list[dict]:  # pragma: no cover - 被子类覆盖
        raise NotImplementedError


class ClaudeAdapter(Adapter):
    """Claude Code: 双向 stream-json, 是三个里最完整的一个。

    事件形状 (实测):
      {"type":"system","subtype":"init","session_id":...}
      {"type":"stream_event","event":{"type":"content_block_delta",
                                      "delta":{"type":"text_delta","text":...}}}
      {"type":"result","session_id":...,"usage":{...},"result":"..."}
    """

    def __init__(self) -> None:
        super().__init__(name="Claude Code", exe="/usr/local/bin/claude", stdin_prompt=True)

    def argv(self, prompt: str, resume: str | None) -> list[str]:
        argv = [
            self.exe,
            "--print",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            # 容器是每用户独占的, 前面还压着我们的 forward_auth —— 再让用户逐条
            # 确认工具调用, 等于把"云端 agent"变成"你得一直盯着的 agent"。
            # 隔离边界是容器本身, 不是这个开关。
            "--permission-mode", "bypassPermissions",
        ]
        if resume:
            argv += ["--resume", resume]
        return argv

    def stdin_payload(self, prompt: str) -> str:
        return json.dumps(
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
            ensure_ascii=False,
        ) + "\n"

    def feed(self, line: str) -> list[dict]:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return [{"t": "raw", "line": line}]
        typ = e.get("type")
        out: list[dict] = []
        if typ == "system" and e.get("subtype") == "init":
            out.append({"t": "session", "id": e.get("session_id", "")})
        elif typ == "stream_event":
            ev = e.get("event") or {}
            et = ev.get("type")
            if et == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    out.append({"t": "delta", "text": d.get("text", "")})
                elif d.get("type") == "thinking_delta":
                    out.append({"t": "thinking", "text": d.get("thinking", "")})
            elif et == "content_block_start":
                blk = ev.get("content_block") or {}
                if blk.get("type") == "tool_use":
                    out.append({"t": "tool", "id": blk.get("id", ""), "name": blk.get("name", ""), "input": {}})
        elif typ == "user":
            # 工具结果是以 user 消息回来的
            for blk in ((e.get("message") or {}).get("content") or []):
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    out.append({
                        "t": "tool_end",
                        "id": blk.get("tool_use_id", ""),
                        "ok": not blk.get("is_error"),
                        "output": _as_text(blk.get("content")),
                    })
        elif typ == "assistant":
            # 工具调用的完整入参只有这里才有 (stream 里 content_block_start 是空的)
            for blk in ((e.get("message") or {}).get("content") or []):
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    out.append({"t": "tool", "id": blk.get("id", ""), "name": blk.get("name", ""),
                                "input": blk.get("input") or {}})
        elif typ == "result":
            u = e.get("usage") or {}
            self.usage = {
                "input": int(u.get("input_tokens") or 0),
                "cache_read": int(u.get("cache_read_input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "model": next(iter((e.get("modelUsage") or {}).keys()), ""),
            }
            if e.get("is_error"):
                out.append({"t": "error", "message": str(e.get("result") or "运行失败")})
            out.append({"t": "done", "usage": self.usage, "session": e.get("session_id", "")})
        return out


class CodexAdapter(Adapter):
    """Codex: `codex exec --json`, 单向 JSONL。

    事件形状 (实测):
      {"type":"thread.started","thread_id":...}
      {"type":"item.completed","item":{"type":"agent_message","text":...}}
      {"type":"turn.completed","usage":{...}}

    注意它**没有流式增量** —— 正文是一整段 item.completed 给的。所以走 "text"
    而不是 "delta", 前端据此不画光标动画, 免得看起来像卡住了。
    """

    def __init__(self) -> None:
        super().__init__(name="Codex", exe="/usr/local/bin/codex")

    def argv(self, prompt: str, resume: str | None) -> list[str]:
        argv = [self.exe, "exec", "--json", "--skip-git-repo-check",
                "--sandbox", "danger-full-access"]
        if resume:
            argv += ["resume", resume]
        argv.append(prompt)
        return argv

    def feed(self, line: str) -> list[dict]:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            return [{"t": "raw", "line": line}]
        typ = e.get("type")
        if typ == "thread.started":
            return [{"t": "session", "id": e.get("thread_id", "")}]
        if typ == "item.completed":
            item = e.get("item") or {}
            it = item.get("type")
            if it == "agent_message":
                return [{"t": "text", "text": item.get("text", "")}]
            if it == "reasoning":
                return [{"t": "thinking", "text": item.get("text", "")}]
            if it in ("command_execution", "file_change", "tool_call"):
                return [{"t": "tool", "id": item.get("id", ""), "name": it,
                         "input": {k: v for k, v in item.items() if k not in ("id", "type")}}]
        if typ == "turn.completed":
            u = e.get("usage") or {}
            self.usage = {
                "input": int(u.get("input_tokens") or 0),
                "cache_read": int(u.get("cached_input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "model": "",
            }
            return [{"t": "done", "usage": self.usage}]
        if typ == "turn.failed" or typ == "error":
            return [{"t": "error", "message": json.dumps(e, ensure_ascii=False)[:400]}]
        return [{"t": "raw", "line": line}]


class GeminiAdapter(Adapter):
    """Gemini CLI: `-o stream-json`。

    ⚠️ 它的模型路由会绕过我们钉的型号去要它内置的那个 (实测 classifier 走
    gemini-3.1-flash-lite), 而网关只放行在售目录 —— 所以这个适配器**在型号路由
    治好之前不要挂成产品**, 留着是为了架构不欠债。
    """

    def __init__(self) -> None:
        super().__init__(name="Gemini", exe="/usr/local/bin/gemini")

    def argv(self, prompt: str, resume: str | None) -> list[str]:
        argv = [self.exe, "-o", "stream-json", "--approval-mode", "yolo", "--skip-trust"]
        if resume:
            argv += ["--session-id", resume]
        argv += ["-p", prompt]
        return argv

    def feed(self, line: str) -> list[dict]:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            # 它的 stderr/stdout 混着大量非 JSON 噪声 (启动警告、路由日志), 这些
            # 不该冒到前端去 —— 只在明显是错误时才报。
            if "Error" in line or "error" in line:
                return [{"t": "raw", "line": line}]
            return []
        typ = e.get("type")
        if typ == "init":
            return [{"t": "session", "id": e.get("session_id", "")}]
        if typ == "message" and e.get("role") == "assistant":
            return [{"t": "text", "text": _as_text(e.get("content"))}]
        if typ == "tool_call":
            return [{"t": "tool", "id": str(e.get("id", "")), "name": e.get("name", ""),
                     "input": e.get("args") or {}}]
        if typ in ("result", "finish", "done"):
            u = e.get("usage") or {}
            self.usage = {
                "input": int(u.get("promptTokenCount") or u.get("input_tokens") or 0),
                "cache_read": int(u.get("cachedContentTokenCount") or 0),
                "output": int(u.get("candidatesTokenCount") or u.get("output_tokens") or 0),
                "model": e.get("model", ""),
            }
            return [{"t": "done", "usage": self.usage}]
        if typ == "error":
            return [{"t": "error", "message": _as_text(e.get("message")) or json.dumps(e)[:300]}]
        return []


def _as_text(v) -> str:
    """content 有时是字符串, 有时是块数组 —— 两种都要能读。"""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(b.get("text", "") for b in v if isinstance(b, dict))
    if isinstance(v, dict):
        return v.get("text", "")
    return ""


ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter, "gemini": GeminiAdapter}
