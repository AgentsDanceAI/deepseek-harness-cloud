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
import time
from dataclasses import asdict

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, filmdir, rooms, tools

WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Agents Team")
store = rooms.Store()

#: 一次只跑一轮。同一个房间并发两轮会让两批机器人对着**同一份还没写回去的记录**
#: 各说各话, 谁也看不见谁 —— 而群聊里"两个人同时回答同一个问题却互不知情"看着
#: 就是产品坏了。跨房间也串行: 它们共用同一个容器和 /workspace, 真并行起来
#: 会互相踩文件。
_turn_lock = asyncio.Lock()
#: 这一轮是什么时候开始的 (0 = 空闲)。锁本身不带时间戳, 而"锁卡死了"只能靠
#: 时间判断 —— 见 send() 里的说明。
_turn_started: float = 0.0
#: 超过这么久还占着锁, 认定上一轮已经死了, 新的一轮可以强行接管。
#: 取值要大于最慢的一轮: 出片一条几分钟, 一棒十几个镜头可能跑半小时。
TURN_STALE_S = float(os.environ.get("AGENTS_TEAM_TURN_STALE", "2400"))


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
                # 消息数 —— 房间重名时这是唯一分得出"哪个是我刚才那个"的线索
                # (2026-09-01 老板: 切出去再回来"消息没了", 其实是切到了另一个同名房间)
                "count": len(store.transcript(r.id)),
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


@app.post("/api/rooms/crew")
async def create_crew(request: Request) -> dict:
    """开一部新片: 剧组五个工位按接力顺序入群, 房间是 relay 模式。

    对应千问那个"自动组队" —— 用户提一个想法就该开工, 不该先手工拉五个人,
    更不该自己记住谁先谁后。
    """
    body = await request.json() if await request.body() else {}
    name = (body.get("name") or "").strip()
    return {"room": asdict(store.create_crew_room(name))}


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


def _enter_film_dir(room: rooms.Room) -> None:
    """把这一轮钉进**这部片自己的目录**, 并保证它存在。

    不 reset: 这两个函数是异步生成器, 每个房间的一轮跑在自己的任务里, contextvar
    本来就不跨任务泄漏; 而在生成器里 reset 要靠 finally, 浏览器一断开生成器被丢弃
    时未必跑得到 —— 那正是轮次锁卡死过的形状 (见 send 里的注释)。
    """
    here = filmdir.resolve(getattr(room, "dir", "") or "")
    here.mkdir(parents=True, exist_ok=True)
    filmdir.use(here)


async def _run_relay(room: rooms.Room, model: str | None):
    """接力: 按 members 顺序**逐棒**跑, 后一位看得见前一位这一轮刚说的话。

    这是流水线成立的前提。并行模式下几个成员拿到的是同一份旧记录, 谁也看不见谁
    —— 美术读不到导演刚写的讲戏本, 分镜读不到美术刚出的资产清单, 五个人对着
    同一句"做个短剧"各说各话。接力把每一棒的产出**先落进记录再传棒**, 下一位
    render_for 时自然就看见了。

    两处会停:
      · 有人调了 wait_for_user —— 人审闸。后面的工位这一轮不开工 (最典型的是
        分镜交完镜头表: 下一棒就要烧钱出片了)。用工具而不是文本标记来判定,
        是因为标记会漏会被改写, 而工具调用漏不了。
      · 有人炸了 —— 半截的产物传下去只会让后面的人基于错的东西接着做。

    **停完从哪续**: 从被叫停的**下一棒**接着跑 (room.resume_at), 不从头重来。
    从头重来的代价不只是慢 —— 美术会照着"再做一遍资产"的字面意思**再出一遍图**,
    那是真花钱; 分镜也可能把用户刚确认过的表重写一遍。
    """
    _enter_film_dir(room)
    members = list(room.members)
    start = room.resume_at if 0 <= room.resume_at < len(members) else 0
    if start:
        yield {"type": "resume", "from": members[start], "skipped": start}
    for idx in range(start, len(members)):
        bot_id = members[idx]
        bot = store.bots.get(bot_id)
        if bot is None:
            continue
        halted = False
        capped_here = False
        try:
            view = store.render_for(bot, room.id)
            async for ev in agent.run_turn(bot.id, view, bot.model or model):
                if ev["type"] == "end":
                    text = (ev.get("text") or "").strip()
                    used = ev.get("tools") or []
                    if text:
                        # **先落记录再传棒** —— 下一位的 render_for 要读得到
                        store.add(room.id, ev["bot"], text, used)
                    if any(tools.HALT_TOOL in str(t) for t in used):
                        halted = True
                    # **空棒**: 一个字没说、一个工具没动 = 这一棒根本没跑成 (最常见
                    # 的成因是网关空流)。传下去等于让后面的人对着不存在的产物开工
                    # —— 2026-09-01 首跑: 导演空棒, 美术和分镜读了十几次从没写出来
                    # 的讲戏本, 全程无一处报错。当"没干完"处理: 停住, 且续跑重跑本棒。
                    elif not text and not used:
                        yield {
                            "type": "error",
                            "bot": bot_id,
                            "message": "这一棒是空的 (没说话, 也没动工具) — 已停住, 没往下传棒",
                        }
                        halted = True
                        capped_here = True
                    # 撞步数上限 = 这一棒**没干完**, 也要停 (别让下游拿半成品接着做),
                    # 且续跑要重跑**这一棒** —— 它还有活没干完。
                    if ev.get("capped"):
                        halted = True
                        capped_here = True
                elif ev["type"] == "error":
                    # **炸了就停, 别传棒。** 上面那句 except 只接得住**抛出来**的
                    # 异常, 而网关失败是 run_turn **yield 一个 error 事件**再正常
                    # 结束 —— 于是它被原样转发, 循环若无其事地跑下一位。接力"有人
                    # 炸了就停"的本意, 对**最常见的那种失败**一直没生效。
                    # 中断前说过的话要落进记录: 那些字已经流到浏览器了, 不落盘的话
                    # 屏幕上有、记录里没有, 续跑时这一棒等于什么都没说过。
                    if (ev.get("said") or "").strip():
                        store.add(room.id, bot_id,
                                  ev["said"] + "\n\n*(这一棒被网关中断, 以上是断线前说完的部分)*",
                                  ev.get("tools") or [])
                    halted = True
                    capped_here = True   # 活没干完 —— 续跑重跑本棒
                yield ev
        except Exception as e:  # noqa: BLE001 — 一棒炸了就停, 别让下游基于半成品接着做
            # 停在**炸掉的这一棒**上 (不是下一棒): 它的活没干完, 重发时要重跑它
            room.resume_at = idx
            store.save()
            yield {"type": "error", "bot": bot_id, "message": f"{type(e).__name__}: {e}"}
            return
        if halted:
            # 下次从**下一棒**接着跑 (这一棒已经交活了, 它只是在等回话)
            # 撞上限停在**本棒** (活没干完, 说"继续"要接着它干);
            # 人审闸停在**下一棒** (这一棒交活了, 只是在等回话)。
            nxt = idx if capped_here else idx + 1
            room.resume_at = nxt if nxt < len(members) else -1
            yield {"type": "halted", "bot": bot_id, "resume_at": room.resume_at}
            store.save()
            return
    room.resume_at = -1   # 整条跑完 —— 下一轮是新需求, 从头开始
    store.save()


async def _run_room(room: rooms.Room, model: str | None):
    """让房间里的成员**同时**跑各自的一轮, 把事件合成一条流。

    并行的代价说清楚: 同一轮里几个成员是**对着用户说话, 不是对着彼此** —— 它们
    在开跑那一刻拿到的是同一份记录, 谁也看不见谁这一轮说了什么。要它们接话, 就得
    再发一轮 (那时上一轮的话已经在记录里了)。这是"同时出结果"换来的, 不是 bug;
    改成串行就变回"排队发言", 那正是我们不想要的形态。
    """
    _enter_film_dir(room)
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
        # ⚠️ 锁必须**能自己恢复**。这里包的是一个流式生成器: 浏览器一断
        # (关标签页/切走/网络抖动/容器被换), 生成器不保证被正常关闭, 锁就永远
        # 不释放 —— 之后**所有房间**的每一条消息都被"上一轮还在跑"挡死, 而界面
        # 上那句话还看不见, 表现成"消息发出去就没了"。
        # 2026-09-01 老板实测: 连发四条「继续」零回应, 全卡在这把锁上。
        #
        # 两道保险:
        #   · 陈旧锁自动放行 —— 超过 TURN_STALE_S 没有心跳就认定上一轮已经死了;
        #   · finally 里无条件释放, 不依赖 async with 的正常退出路径。
        global _turn_started
        stale = _turn_started and (time.time() - _turn_started) > TURN_STALE_S
        if _turn_lock.locked() and not stale:
            waited = int(time.time() - (_turn_started or time.time()))
            yield _sse({"type": "error",
                        "message": f"上一轮还在跑 ({waited} 秒), 等它结束再发。"})
            return
        if stale:
            # 上一轮已经死了 (多半是浏览器断开时生成器没被关掉)。强行接管。
            try:
                _turn_lock.release()
            except RuntimeError:
                pass
        await _turn_lock.acquire()
        _turn_started = time.time()
        try:
            store.add(room_id, "user", text)
            store.save()
            runner = _run_relay if room.mode == "relay" else _run_room
            async for ev in runner(room, model):
                yield _sse(ev)
        finally:
            _turn_started = 0.0
            try:
                _turn_lock.release()
            except RuntimeError:
                pass

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
