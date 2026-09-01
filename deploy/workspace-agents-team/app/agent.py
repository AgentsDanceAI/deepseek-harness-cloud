"""单个机器人的一轮工具循环。

**它不知道有房间, 也不知道有前端。** 输入是一份已经渲染好的 messages (谁渲染的、
按谁的视角, 是 rooms.py 的事), 输出是一串带 bot 标签的事件。这条边界是为了并行:
一个房间里几个机器人同时跑, 各自是一个独立的本协程, 互不共享状态 —— 共享的只有
房间记录, 而那份由调用方在每轮**结束时**写入。

事件:
    {"type":"text",   "bot", "text"}      正文增量
    {"type":"tool",   "bot", "id","name","args"}
    {"type":"result", "bot", "id","summary"}
    {"type":"image",  "bot", "id","data_uri"}
    {"type":"end",    "bot", "text","tools","usage"}   本轮说完了
    {"type":"error",  "bot", "message"}
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx

from . import tools

GATEWAY_BASE = os.environ.get("DSH_GATEWAY_BASE", "").rstrip("/")
GATEWAY_TOKEN = os.environ.get("DSH_CLOUD_TOKEN", "")
DEFAULT_MODEL = os.environ.get("DSH_DEFAULT_MODEL", "")

#: 一轮里最多来回多少次。到顶不是报错, 是**把话语权还回去** —— 智能体绕进死循环
#: 时报错只会让人重来一遍再绕一次, 而摊开说"我做到这里了"他能接手。
MAX_STEPS = int(os.environ.get("AGENTS_TEAM_MAX_STEPS", "30"))
#: 出片/出图的工位要按镜头逐条跑 —— 一部三分钟短剧就是三四十个镜头, 三十步的
#: 通用上限**必然**把它掐在半路 (2026-08-31: 老板问"中间还会停顿吗", 查出来的
#: 第二处非设计停顿)。给这几位单独放宽; 其余工位维持 30 步 —— 那个上限是防
#: 跑飞的, 不该为一个特例整体放开。
LONG_RUN_BOTS = {"videographer", "artist", "editor"}
LONG_RUN_STEPS = int(os.environ.get("AGENTS_TEAM_LONG_STEPS", "120"))

#: 群聊里额外压一层通用约束。人格由 rooms.render_for 拼在前面, 这里只放**与形态
#: 有关**的部分 —— 人格是产品配置, 这段是机制。
SHARED_RULES = """
你跑在一个真实的 Linux 容器里: 能装软件、跑脚本、读写文件、访问网络。
工作目录 /workspace 挂在持久存储上, 其它位置写的东西会随实例回收消失。

- 先动手看再下判断。要知道什么东西在不在, 跑一条命令看, 别猜。
- 一次一步, 每步结果决定下一步; 别把十条命令拼成一条巨型流水线。
- 说你**做了什么、看到了什么**, 别复述你打算做什么。
- 用中文, 除非用户用别的语言。
"""


class GatewayError(RuntimeError):
    pass


async def _stream(
    client: httpx.AsyncClient, model: str, messages: list[dict]
) -> AsyncIterator[dict]:
    """要一次流式补全。

    **工具调用是分片到达的**: 同一个 tool_call 的 name 和 arguments 跨多个 chunk
    送来, 用 `index` 归位。按到达顺序拼是错的 —— 并行工具调用时几个 index 交错
    出现, 顺序拼会把 A 的参数接到 B 上, 而 JSON 照样解析成功, 于是"参数对不上
    却不报错"。
    """
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools.SCHEMAS,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    async with client.stream(
        "POST",
        f"{GATEWAY_BASE}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
        timeout=httpx.Timeout(300.0, connect=15.0),
    ) as r:
        if r.status_code != 200:
            body = (await r.aread()).decode("utf-8", "replace")[:300]
            raise GatewayError(f"网关返回 {r.status_code}: {body}")
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue  # 网关偶尔插一行非 JSON 心跳; 跳过比炸掉整轮好


async def run_turn(
    bot_id: str, messages: list[dict], model: str | None = None
) -> AsyncIterator[dict]:
    """跑一个机器人的一轮, 直到它不再调用工具。

    `messages` 是**这一轮的工作副本**, 会被就地追加 —— 调用方不该复用它。
    """
    mdl = model or DEFAULT_MODEL
    max_steps = LONG_RUN_STEPS if bot_id in LONG_RUN_BOTS else MAX_STEPS
    if not GATEWAY_BASE or not GATEWAY_TOKEN:
        yield {
            "type": "error",
            "bot": bot_id,
            "message": "工作台没拿到网关凭据, 请重开工作台。",
        }
        return

    # 机制约束追加在人格之后 (人格在 messages[0])
    if messages and messages[0].get("role") == "system":
        messages[0] = {
            "role": "system",
            "content": messages[0]["content"] + "\n" + SHARED_RULES,
        }

    said: list[str] = []
    ran: list[str] = []
    usage: dict = {}

    async with httpx.AsyncClient() as client:
        for _ in range(max_steps):
            parts: list[str] = []
            calls: dict[int, dict] = {}
            try:
                async for chunk in _stream(client, mdl, messages):
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        d = choice.get("delta") or {}
                        if d.get("content"):
                            parts.append(d["content"])
                            yield {"type": "text", "bot": bot_id, "text": d["content"]}
                        for tc in d.get("tool_calls") or []:
                            slot = calls.setdefault(
                                tc.get("index", 0), {"id": "", "name": "", "args": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["args"] += fn["arguments"]
            except (GatewayError, httpx.HTTPError) as e:
                yield {"type": "error", "bot": bot_id, "message": str(e)}
                return

            text = "".join(parts)
            if text.strip():
                said.append(text.strip())
            if not calls:
                yield {
                    "type": "end",
                    "bot": bot_id,
                    "text": "\n".join(said),
                    "tools": ran,
                    "usage": usage,
                }
                return

            ordered = [calls[i] for i in sorted(calls)]
            messages.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": c["args"] or "{}",
                            },
                        }
                        for c in ordered
                    ],
                }
            )

            for c in ordered:
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    # 参数没拼成合法 JSON 时把原文还给模型让它重来, 不要静默塞空字典 ——
                    # 那会让它以为工具跑过了, 然后基于空结果往下编。
                    body, summary, args = (
                        f"参数不是合法 JSON: {c['args'][:200]}",
                        "参数错误",
                        None,
                    )
                if args is not None:
                    yield {
                        "type": "tool",
                        "bot": bot_id,
                        "id": c["id"],
                        "name": c["name"],
                        "args": args,
                    }
                    body, summary = await tools.dispatch(c["name"], args)

                if body.startswith("data:image/"):
                    yield {
                        "type": "image",
                        "bot": bot_id,
                        "id": c["id"],
                        "data_uri": body,
                    }
                    body = "(截图已展示给用户)"
                ran.append(summary)
                yield {
                    "type": "result",
                    "bot": bot_id,
                    "id": c["id"],
                    "summary": summary,
                }
                messages.append(
                    {"role": "tool", "tool_call_id": c["id"], "content": body}
                )

    yield {
        "type": "end",
        "bot": bot_id,
        "text": "\n".join(said),
        "tools": ran,
        "usage": usage,
        # 说清楚"还没干完"而不只是"停了" —— 用户据此知道该说一句"继续",
        # 而不是以为它已经交活了 (被掐时正文往往看起来像正常收尾)。
        "note": f"连做了 {max_steps} 步还没收尾, 先停下来 — 回一句「继续」可以接着干。",
        "capped": True,
    }
