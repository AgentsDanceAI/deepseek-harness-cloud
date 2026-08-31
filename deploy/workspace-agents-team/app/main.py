"""Operator 的 HTTP 层: 房间、成员、以及一条把并行机器人合流的 SSE。

**没有账号体系, 也不该有。** 老板铁律是接进来的应用一律不留登录墙, 而这个域前面
压着我们自己的 forward_auth —— 用户走到这里已经登过一次了。再加一层等于第二道墙,
还会重演 Dify/Hermes 那类"会话过期就把人永久锁在登录页"的事故。容器本身每用户
独占, 那才是隔离边界。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, rooms

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Agents Team")
store = rooms.Store()

#: 一次只跑一轮。同一个房间并发两轮会让两批机器人对着**同一份还没写回去的记录**
#: 各说各话, 谁也看不见谁 —— 而群聊里"两个人同时回答同一个问题却互不知情"看着
#: 就是产品坏了。跨房间也串行: 它们共用同一个容器和 /workspace, 真并行起来
#: 会互相踩文件。
_turn_lock = asyncio.Lock()


@app.get("/api/health")
def health() -> dict:
    """就绪探针打这里。

    别拿首页当判据: 首页是静态文件, 后端没起来它照样 200 —— 那样探针会在应用
    真正可用之前放人进来 (2026-08-30 Dify/Coze 都栽过)。
    """
    return {"ok": True, "gateway": bool(agent.GATEWAY_BASE), "rooms": len(store.rooms)}


@app.get("/api/models")
def models() -> dict:
    listed = [m for m in os.environ.get("DSH_MODELS", "").split() if m]
    return {"models": listed or [agent.DEFAULT_MODEL], "default": agent.DEFAULT_MODEL}


@app.get("/api/bots")
def bots() -> dict:
    return {"bots": [asdict(b) for b in store.bots.values()]}


@app.get("/api/rooms")
def list_rooms() -> dict:
    return {
        "rooms": [
            {
                **asdict(r),
                "last": next(
                    (
                        m.text[:40]
                        for m in reversed(store.transcript(r.id))
                        if m.text.strip()
                    ),
                    "",
                ),
            }
            for r in sorted(store.rooms.values(), key=lambda x: x.created)
        ]
    }


@app.post("/api/rooms")
async def create_room(request: Request) -> dict:
    body = await request.json()
    members = [m for m in (body.get("members") or []) if m in store.bots]
    if not members:
        return {"error": "至少要拉一个机器人进来"}
    name = (body.get("name") or "").strip() or "、".join(
        store.bots[m].name for m in members
    )
    return {"room": asdict(store.create_room(name, members))}


@app.post("/api/rooms/{room_id}/members")
async def set_members(room_id: str, request: Request) -> dict:
    """拉人进群 / 请人出群。

    新成员**看得见入群前的全部记录** (render_for 每轮现渲染整份记录) —— 群聊里
    "新来的读不到上文"是致命的, 所以这里不需要给他补发历史。
    """
    room = store.rooms.get(room_id)
    if room is None:
        return {"error": "没有这个房间"}
    body = await request.json()
    room.members = [m for m in (body.get("members") or []) if m in store.bots]
    store.save()
    return {"room": asdict(room)}


@app.get("/api/rooms/{room_id}/messages")
def messages(room_id: str) -> dict:
    return {"messages": [asdict(m) for m in store.transcript(room_id)]}


async def _run_room(room: rooms.Room, model: str | None):
    """让房间里的成员**同时**跑各自的一轮, 把事件合成一条流。

    并行的代价说清楚: 同一轮里几个成员是**对着用户说话, 不是对着彼此** —— 它们
    在开跑那一刻拿到的是同一份记录, 谁也看不见谁这一轮说了什么。要它们接话, 就得
    再发一轮 (那时上一轮的话已经在记录里了)。这是"同时出结果"换来的, 不是 bug;
    改成串行就变回"排队发言", 那正是我们不想要的形态。
    """
    members = [store.bots[m] for m in room.members if m in store.bots]
    queue: asyncio.Queue = asyncio.Queue()

    async def drive(bot: rooms.Bot) -> None:
        try:
            view = store.render_for(bot, room.id)
            async for ev in agent.run_turn(bot.id, view, bot.model or model):
                await queue.put(ev)
        except Exception as e:  # noqa: BLE001 — 一个成员炸了不该拖垮整个房间
            await queue.put(
                {"type": "error", "bot": bot.id, "message": f"{type(e).__name__}: {e}"}
            )
        finally:
            await queue.put({"type": "_done", "bot": bot.id})

    tasks = [asyncio.create_task(drive(b)) for b in members]
    left = len(tasks)
    try:
        while left:
            ev = await queue.get()
            if ev["type"] == "_done":
                left -= 1
                continue
            if ev["type"] == "end":
                text = (ev.get("text") or "").strip()
                if text:
                    store.add(room.id, ev["bot"], text, ev.get("tools") or [])
            yield ev
    finally:
        for t in tasks:
            t.cancel()
        store.save()


@app.post("/api/rooms/{room_id}/send")
async def send(room_id: str, request: Request) -> StreamingResponse:
    body = await request.json()
    text = (body.get("message") or "").strip()
    model = body.get("model") or None

    async def stream():
        room = store.rooms.get(room_id)
        if room is None:
            yield _sse({"type": "error", "message": "没有这个房间"})
            return
        if not text:
            yield _sse({"type": "error", "message": "空消息"})
            return
        if _turn_lock.locked():
            yield _sse({"type": "error", "message": "上一轮还在跑, 等它结束再发。"})
            return
        async with _turn_lock:
            store.add(room_id, "user", text)
            store.save()
            async for ev in _run_room(room, model):
                yield _sse(ev)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    import json as _json

    return f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
