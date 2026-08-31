"""Operator 的智能体主循环。

对外只有一个出口: `run_turn()` 是个异步生成器, 吐出一串事件。前端、日志、以后
可能的其他壳子都只认这串事件 —— 循环本身不知道有没有浏览器在看。这条边界是
故意画的: 换前端、加个 API、跑批处理, 都不用动循环。

事件类型:
    {"type": "text",  "text": ...}          模型正文增量
    {"type": "tool",  "id","name","args"}   决定调用某工具 (参数已完整)
    {"type": "result","id","summary","ok"}  工具执行完 (正文不回前端, 太长)
    {"type": "image", "id","data_uri"}      工具产出的图 (截图)
    {"type": "end",   "usage": {...}}       本轮结束
    {"type": "error", "message": ...}       本轮失败
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

#: 一轮里最多来回多少次。到顶不是报错, 是**把话语权还给用户** —— 智能体绕进
#: 死循环时, 报错只会让人重来一遍再绕一次, 而摊开说"我做到这里了"他能接手。
MAX_STEPS = int(os.environ.get("OPERATOR_MAX_STEPS", "40"))

SYSTEM_PROMPT = """你是 DSH Cloud 的操作员智能体, 跑在用户自己的云工作台容器里。

你有一个真实的 Linux 环境: 能装软件、跑脚本、读写文件、访问网络。工作目录
/workspace 挂在持久存储上, 实例回收后还在; 其它位置写的东西会随容器消失。

怎么干活:
- 先动手看, 再下判断。要知道某个东西在不在, 跑一条命令看, 别猜。
- 一次一步, 每步的结果决定下一步。不要把十条命令拼成一条巨型流水线 ——
  中间哪一步错了你和用户都看不出来。
- 命令失败时先读报错再改, 不要把同一条命令换个写法反复重试。
- 装东西优先用系统包管理器或语言自带的包管理器, 装之前先看看是不是已经有了。

怎么说话:
- 用中文回答, 除非用户用别的语言。
- 说你**做了什么、看到了什么**, 不要复述你打算做什么。
- 做完给结论, 不要把终端输出整段粘回去 —— 用户要的是结果, 不是日志。
"""


class GatewayError(RuntimeError):
    pass


def _messages(history: list[dict]) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, *history]


async def _stream_completion(
    client: httpx.AsyncClient, model: str, history: list[dict]
) -> AsyncIterator[dict]:
    """向网关要一次流式补全, 逐条吐出 delta。

    **工具调用是分片到达的**: 同一个 tool_call 的 name 和 arguments 会跨多个 chunk
    片段送来, 用 `index` 归位。按到达顺序拼是错的 —— 并行工具调用时几个 index
    交错出现, 顺序拼会把 A 的参数接到 B 上, 而 JSON 照样能解析成功,
    于是变成"参数对不上却不报错"。
    """
    payload = {
        "model": model,
        "messages": _messages(history),
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
            body = (await r.aread()).decode("utf-8", "replace")[:400]
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
                # 网关偶尔会插一行非 JSON 的心跳; 跳过比炸掉整轮好。
                continue


async def run_turn(history: list[dict], model: str | None = None) -> AsyncIterator[dict]:
    """跑一轮对话直到模型不再调用工具。

    `history` 会被**就地追加** —— 调用方拿到的就是更新后的对话, 不用自己拼。
    """
    mdl = model or DEFAULT_MODEL
    if not GATEWAY_BASE or not GATEWAY_TOKEN:
        yield {"type": "error", "message": "工作台没有拿到网关凭据, 请重开工作台。"}
        return

    usage: dict = {}
    async with httpx.AsyncClient() as client:
        for _ in range(MAX_STEPS):
            text_parts: list[str] = []
            calls: dict[int, dict] = {}
            try:
                async for chunk in _stream_completion(client, mdl, history):
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text_parts.append(delta["content"])
                            yield {"type": "text", "text": delta["content"]}
                        for tc in delta.get("tool_calls") or []:
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
                yield {"type": "error", "message": str(e)}
                return

            text = "".join(text_parts)
            if not calls:
                history.append({"role": "assistant", "content": text})
                yield {"type": "end", "usage": usage}
                return

            ordered = [calls[i] for i in sorted(calls)]
            history.append(
                {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": [
                        {
                            "id": c["id"],
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["args"] or "{}"},
                        }
                        for c in ordered
                    ],
                }
            )

            for c in ordered:
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    # 参数没拼成合法 JSON 时**把原文还给模型**让它重来, 不要静默
                    # 塞个空字典 —— 那会让它以为工具跑过了, 然后基于空结果往下编。
                    body, summary = f"参数不是合法 JSON, 原文: {c['args'][:300]}", "参数错误"
                    args = None
                if args is not None:
                    yield {"type": "tool", "id": c["id"], "name": c["name"], "args": args}
                    body, summary = await tools.dispatch(c["name"], args)

                if body.startswith("data:image/"):
                    yield {"type": "image", "id": c["id"], "data_uri": body}
                    # 图不塞进 history 的文本里 —— 多模态消息要按模型的格式走,
                    # 而目录里不是每个模型都支持。这里只留一句说明, 图给前端。
                    body = "(截图已展示给用户)"
                yield {"type": "result", "id": c["id"], "summary": summary, "ok": True}
                history.append({"role": "tool", "tool_call_id": c["id"], "content": body})

    yield {
        "type": "end",
        "usage": usage,
        "note": f"已经连续做了 {MAX_STEPS} 步还没收尾, 先停下来听你的。",
    }
