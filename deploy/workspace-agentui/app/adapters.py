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

    @property
    def term_cmd(self) -> str:
        """点开「终端」标签页时先替用户敲的那条命令。

        默认就是这个 CLI 本身 (claude/codex/gemini 都是敲名字就进交互界面)。
        但**有的接法 exe 是解释器** (OpenManus/CrewAI 是 Python 库, 我们跑的是
        自己的 runner) —— 那时直接敲 exe 会掉进 Python REPL, 得由适配器自己说
        终端里该敲什么。
        """
        return self.exe

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


class JsonlRunnerAdapter(Adapter):
    """跑我们自己写的 runner, 它**直接吐这套统一事件**, 一行一个 JSON。

    与上面三个的区别只在事件从哪来: 那三个 CLI 自带 JSON 流 (--output-format
    stream-json / --json), 而 OpenManus 与 CrewAI 是 Python 库, 没有这种流。
    去刮它们的控制台文本等于把排版当协议 —— 所以改成在 runner 里挂它们自己的
    钩子 (loguru sink / Crew 回调) 直接产出事件。runner 见
    deploy/workspace-frameworks/runner/。

    认不出的行照样走 raw: runner 的 stderr 里还有框架自己的输出, 而**丢掉一行
    的症状是"偶尔少半句话"**, 查起来毫无线索。
    """

    def __init__(self, name: str, exe: str, runner: str, term: str) -> None:
        super().__init__(name=name, exe=exe)
        self._runner = runner
        self._term = term

    def argv(self, prompt: str, resume: str | None) -> list[str]:
        # 没有 --resume: 这两个框架都是**一轮一个进程**, 上下文不由 CLI 端保存。
        # 硬塞一个假的会话 id 只会让前端以为能续上。
        return [self.exe, self._runner, prompt]

    @property
    def term_cmd(self) -> str:
        return self._term

    def feed(self, line: str) -> list[dict]:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return [{"t": "raw", "line": line[:400]}]
        if not isinstance(ev, dict) or "t" not in ev:
            return [{"t": "raw", "line": line[:400]}]
        if ev["t"] == "done":
            # **用量的键名必须是 input/output** —— 前端读的就是这两个。写成各家
            # API 那套 *_tokens / prompt_tokens 不会报任何错, 只是"本轮消耗"
            # 永远显示 0↑0↓ (2026-09-02 上线当天就是这么漏出去的)。
            # runner 已按约定吐, 这里再兜一层: 它哪天漂了也不至于静默归零。
            u = dict(ev.get("usage") or {})
            for house, aliases in (("input", ("input_tokens", "prompt_tokens")),
                                   ("output", ("output_tokens", "completion_tokens"))):
                if house not in u:
                    for a in aliases:
                        if a in u:
                            u[house] = u[a]
                            break
            self.usage = u
            ev["usage"] = u
        return [ev]


class OpenManusAdapter(JsonlRunnerAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="OpenManus",
            exe="/opt/venv-openmanus/bin/python",
            runner="/opt/dsh/openmanus_run.py",
            # 终端里给的是**它自己的交互入口**, 不是我们的 runner —— 用户在终端
            # 里要的是原汁原味的 OpenManus。
            term="cat /etc/motd 2>/dev/null; cd /opt/openmanus && /opt/venv-openmanus/bin/python main.py",
        )


class CrewAIAdapter(JsonlRunnerAdapter):
    def __init__(self) -> None:
        super().__init__(
            name="CrewAI",
            exe="/opt/venv-crewai/bin/python",
            runner="/opt/dsh/crewai_run.py",
            # 终端里落到工程目录: 用户在这儿 `crewai run` / 改 yaml, 跑的和左边
            # 对话是同一份 crew。
            term="cat /etc/motd 2>/dev/null; cd /workspace/crew",
        )


ADAPTERS = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "gemini": GeminiAdapter,
    "openmanus": OpenManusAdapter,
    "crewai": CrewAIAdapter,
}
