"""Operator 工作台的 HTTP 层: 一个 SSE 聊天端点 + 静态前端。

**没有账号体系, 也不该有。** 老板的铁律是接进来的应用一律不留登录墙, 而这个域
前面压着我们自己的 forward_auth —— 用户走到这里已经登过一次了。再加一层等于
第二道墙, 而且会重演 Dify/Hermes 那类"会话过期就把人永久锁在登录页"的事故。
容器本身是每用户独占的, 那才是隔离边界。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, tools

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"

#: 对话落盘在 NAS 上 —— 工作台一回收容器就没了, 而用户会指望"回来还在"。
#: 放在 /workspace 下的隐藏目录: 它已经挂了持久卷, 不用再多要一个挂载点。
STATE_PATH = tools.WORKDIR / ".operator" / "conversation.json"

app = FastAPI(title="DSH Operator")

#: 进程内的对话锁。一个工作台就一个用户, 但他会开两个标签页 —— 两轮并发跑同一份
#: history 会把 tool_calls 和 tool 结果交错写坏, 而 OpenAI 格式要求两者严格配对,
#: 坏了之后**每一轮都 400**, 且清空对话前恢复不了。
_lock = asyncio.Lock()


def _load() -> list[dict]:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _save(history: list[dict]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False))
    tmp.replace(STATE_PATH)  # 原子替换: 写一半被杀不会留下半个 JSON


@app.get("/api/health")
def health() -> dict:
    """就绪探针打这里。

    别拿首页当判据: 首页是静态文件, 后端还没起来它照样 200 —— 那样探针会在
    应用真正可用之前就放人进来 (2026-08-30 Dify/Coze 都栽过这个)。
    """
    return {"ok": True, "model": agent.DEFAULT_MODEL, "gateway": bool(agent.GATEWAY_BASE)}


@app.get("/api/models")
def models() -> dict:
    """给前端的模型下拉。列表由工作台在 env 里下发, 与在售目录一致。"""
    listed = [m for m in os.environ.get("DSH_MODELS", "").split() if m]
    return {"models": listed or [agent.DEFAULT_MODEL], "default": agent.DEFAULT_MODEL}


@app.get("/api/conversation")
def conversation() -> dict:
    """回放用: 只给前端**看得见**的那部分 (用户说的和智能体说的)。

    工具消息不回 —— 它们是模型的工作记录, 原文又长又是给机器读的; 前端时间线
    要的是摘要, 那个在流式事件里已经给过了。
    """
    out = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in _load()
        if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()
    ]
    return {"messages": out}


@app.post("/api/reset")
def reset() -> dict:
    with contextlib.suppress(OSError):
        STATE_PATH.unlink()
    return {"ok": True}


@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    body = await request.json()
    text = (body.get("message") or "").strip()
    model = body.get("model") or None

    async def stream():
        if not text:
            yield _sse({"type": "error", "message": "空消息"})
            return
        if _lock.locked():
            yield _sse({"type": "error", "message": "上一轮还在跑, 等它结束再发。"})
            return
        async with _lock:
            history = _load()
            history.append({"role": "user", "content": text})
            try:
                async for event in agent.run_turn(history, model):
                    yield _sse(event)
            finally:
                # 无论正常收尾还是客户端中途断开, 都把已经产生的对话存下来 ——
                # 否则用户刷新一下, 智能体刚做的事在记录里完全不存在。
                _save(history)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
