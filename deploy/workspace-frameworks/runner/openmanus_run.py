"""跑一轮 OpenManus, 把它的日志翻成工作台那套统一事件 (每行一个 JSON)。

为什么不直接跑 `main.py --prompt` 再刮控制台
--------------------------------------------
两条硬理由:

1. **它的日志走 stderr** (loguru 的默认 sink), 而工作台只从 stdout 读事件
   (见 agentui/app/main.py: stderr 只挑含 Error 的行透出来)。原样跑的话
   前端一个字都收不到, 症状是"发了消息没反应"。
2. 刮格式化后的文本等于把"日志排版"当协议。这里改成**挂 loguru 的 sink**:
   拿到的是结构化 record, 上游改颜色/改前缀都不影响。

翻译对照 (标记是实测抓的, 不是照文档写的):
    ✨ Manus's thoughts: <正文>      -> text
    🧰 Tools being prepared: [...]   -> 记下待用工具
    🔧 Tool arguments: {...}         -> 记下入参
    🔧 Activating tool: 'x'...       -> tool
    🎯 Tool 'x' completed ... Result -> tool_end
    Token usage: Input=.. Completion=..  -> 累计用量 (前端换算积分)
    ERROR/CRITICAL                   -> error
    其余                             -> raw (原样进调试面板, 不丢)

认不出的一律走 raw —— 丢掉的话症状是"偶尔少半句话", 查起来毫无线索。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

OPENMANUS = os.environ.get("DSH_OPENMANUS_DIR", "/opt/openmanus")
sys.path.insert(0, OPENMANUS)
os.chdir(OPENMANUS)

#: 事件只往**真正的 stdout** 写。框架和它的工具会 print, 那些必须挪开, 否则
#: 一行普通输出就把 JSON 流冲断了。
_OUT = sys.stdout
sys.stdout = sys.stderr


def emit(ev: dict) -> None:
    print(json.dumps(ev, ensure_ascii=False), file=_OUT, flush=True)


_USAGE: dict = {}
_PENDING: dict = {"tools": [], "args": ""}

_THOUGHTS = re.compile(r"Manus's thoughts:\s*(.*)$", re.S)
_PREPARE = re.compile(r"Tools being prepared:\s*(.*)$")
_ARGS = re.compile(r"Tool arguments:\s*(.*)$", re.S)
_ACTIVATE = re.compile(r"Activating tool:\s*'([^']+)'")
_DONE_TOOL = re.compile(r"Tool '([^']+)' completed its mission!\s*Result:\s*(.*)$", re.S)
_TOKENS = re.compile(
    r"Token usage:.*?Cumulative Input=(\d+).*?Cumulative Completion=(\d+).*?Cumulative Total=(\d+)", re.S
)


def _translate(msg: str, level: str) -> list[dict]:
    if level in ("ERROR", "CRITICAL"):
        return [{"t": "error", "message": msg[:600]}]

    m = _TOKENS.search(msg)
    if m:
        # 键名按**工作台的约定**来 (input/output), 不是各家 API 的 *_tokens ——
        # 前端读的就是这两个, 拼错了不会报错, 只是"本轮消耗"永远是 0↑0↓。
        _USAGE.update({"input": int(m.group(1)), "output": int(m.group(2)), "total": int(m.group(3))})
        return []

    m = _THOUGHTS.search(msg)
    if m:
        text = m.group(1).strip()
        # 空想法是常态 (它决定直接调工具时就没有正文), 别往对话里塞空气泡。
        return [{"t": "text", "text": text}] if text else []

    m = _PREPARE.search(msg)
    if m:
        _PENDING["tools"] = m.group(1).strip()
        return []

    m = _ARGS.search(msg)
    if m:
        _PENDING["args"] = m.group(1).strip()
        return []

    m = _ACTIVATE.search(msg)
    if m:
        name = m.group(1)
        return [{"t": "tool", "id": name, "name": name, "input": _PENDING.get("args", "")}]

    m = _DONE_TOOL.search(msg)
    if m:
        return [{"t": "tool_end", "id": m.group(1), "ok": True, "output": m.group(2).strip()[:2000]}]

    return [{"t": "raw", "line": msg[:400]}]


def _sink(message) -> None:
    r = message.record
    for ev in _translate(r["message"], r["level"].name):
        emit(ev)


async def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    if not prompt.strip():
        emit({"t": "error", "message": "没有收到问题"})
        return 2

    # **必须在导入 agent 之前接管 logger**: 各模块都是 `from app.logger import
    # logger` 拿到同一个 loguru 实例, 晚接管就漏掉启动阶段那几行 (其中就有
    # 配置/网关出错时唯一的线索)。
    from app.logger import logger

    logger.remove()
    logger.add(_sink, level="INFO")

    from app.agent.manus import Manus

    agent = await Manus.create()
    try:
        await agent.run(prompt)
    finally:
        # 清理放 finally —— 中断/异常时不清会把 MCP 连接和沙箱留在那儿, 而那种
        # 坏状态不报错, 下一轮才发作。
        try:
            await agent.cleanup()
        except Exception as e:  # noqa: BLE001
            emit({"t": "raw", "line": f"cleanup: {type(e).__name__}: {e}"[:400]})
    emit({"t": "done", "usage": _USAGE})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        emit({"t": "error", "message": f"{type(e).__name__}: {e}"[:600]})
        raise SystemExit(1) from e
