"""数字人 (实时口型视频通话) —— 签发 GPU 节点的令牌, 并按通话分钟计费。

与其它工作台产品**结构上不同**: 它不起每用户容器, 而是转发到我们自己的 GPU 节点
(SoulX-FlashHead 跑在那张 L20 上), 三路并发、满了排队。因此:

  · **计费口径不同**。其它产品收"容器存在时间"的机时额度; 这个没有容器, 收的是
    真实通话分钟的积分。排队等的时间不计费 —— 排队恰恰是因为我们卡不够, 让用户
    为我们的容量不足付钱说不过去。
  · **计费只能由 GPU 侧发起**。只有它知道通话真的开始了没有、还在不在进行中;
    我们这边看到的只是一个 WebSocket 连上了, 那可能还在排队。

信任模型: 两机共享 AVATAR_TOKEN_SECRET。我们签 `ts.<tenant>.<sig>` 给浏览器,
GPU 侧用同一把密钥验; 回报计费时反向签一次, 我们验。租户 id 带 `d-` 前缀与口袋
专家的租户区分开 —— 同一张卡上两条产品线, 名字空间不能混。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from . import config, credits, db
from .accounts import resolve_user, try_resolve_user

router = APIRouter(tags=["avatar"])
log = logging.getLogger("dhc.avatar")

#: DSH Cloud 用户在 GPU 侧的租户前缀。口袋专家用的是自己的租户 id, 两边共用一张
#: 卡 —— 不加前缀的话两个产品线的用户可能撞 id, 而撞了就是**看到别人的形象**。
TENANT_PREFIX = "d-"
#: 令牌有效期。短是故意的: 它只用来建立一次连接, 拿到就该马上用掉。
TOKEN_TTL = 300


def _tenant(user_id: str) -> str:
    return TENANT_PREFIX + user_id


def sign_token(user_id: str) -> str:
    """给浏览器的短时令牌。与 GPU 侧 _check_token 的 v2 格式对齐。"""
    ts = str(int(time.time()))
    tenant = _tenant(user_id)
    sig = hmac.new(config.AVATAR_TOKEN_SECRET.encode(), f"{ts}|{tenant}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{tenant}.{sig}"


@router.get("/api/avatar/session")
def avatar_session(user: dict = Depends(resolve_user)):
    """前端拿这个开始一通电话: 令牌 + GPU 地址 + 单价。

    余额不足**在这里就挡住**, 不放到通话中途 —— 打到一半被掐断比一开始就告诉他
    要糟得多, 而且那时 GPU 的槽位已经占掉了。
    """
    if not config.AVATAR_TOKEN_SECRET:
        raise HTTPException(503, "avatar_not_configured")
    bal = credits.balance(user["id"])
    if bal < config.AVATAR_CREDITS_PER_MIN:
        return JSONResponse(
            status_code=402,
            content={
                "error": "insufficient_credits",
                "balance": bal,
                "credits_per_min": config.AVATAR_CREDITS_PER_MIN,
            },
        )
    return {
        "token": sign_token(user["id"]),
        "gpu": config.AVATAR_GPU_URL,
        "credits_per_min": config.AVATAR_CREDITS_PER_MIN,
        "balance": bal,
    }


@router.post("/api/avatar/meter")
async def avatar_meter(request: Request):
    """GPU 侧按通话分钟回报, 我们扣积分。

    **没有用户会话**: 调用方是 GPU 节点的服务进程, 凭的是共享密钥的签名。所以
    这个端点不能挂 resolve_user —— 它验的是机器身份, 不是人。

    重复投递不会重复扣: 见 _claim_report。
    """
    q = request.query_params
    ts, tenant, minutes, sig = (q.get("ts", ""), q.get("tenant", ""), q.get("minutes", ""), q.get("sig", ""))
    if not _verify_report(ts, tenant, minutes, sig):
        raise HTTPException(401, "bad_signature")
    if not tenant.startswith(TENANT_PREFIX):
        # 口袋专家的租户走它自己那套账, 不该记到 DSH Cloud 头上。
        return {"ok": True, "skipped": "not_a_dsh_tenant"}
    user_id = tenant[len(TENANT_PREFIX) :]
    try:
        mins = max(0, min(int(minutes), 60))  # 单次回报最多算一小时, 防错报
    except ValueError:
        raise HTTPException(400, "bad_minutes") from None
    if mins == 0:
        return {"ok": True, "charged": 0}
    amount = mins * config.AVATAR_CREDITS_PER_MIN
    # 同一份回报被投两次就只收一次。**必须在这里挡**, 不能指望 usage_log 的
    # request_id —— 那一列没有唯一约束, 只是记录字段 (实测: 投两次, 两行同一个
    # request_id, 扣两次钱)。
    if not _claim_report(f"avatar-meter:{tenant}:{ts}:{mins}"):
        log.info("[avatar] 重复回报, 不重复扣 user=%s 分钟=%s", user_id, mins)
        return {"ok": True, "charged": 0, "deduped": True}
    credits.spend(
        user_id,
        amount,
        kind="llm",
        model="avatar:live",
        request_id=f"avatar-{tenant}-{ts}-{mins}",
    )
    log.info("[avatar] 计费 user=%s 分钟=%s 积分=%s", user_id, mins, amount)
    return {"ok": True, "charged": amount}


def _claim_report(key: str) -> bool:
    """第一次见到这份回报 -> 占坑, 返回 True; 见过 -> False。

    **不能拿 usage_log.request_id 当幂等键**: 那一列没有唯一约束, 只是给人看的
    记录字段。先前就是这么写的, 实测同一份回报投两次 -> 两行同 request_id, 扣了
    两次钱。多收钱不报错, 用户也不会知道该来问。

    坑占在 kv 里 (主键即唯一), 插入冲突就说明来过了。**先占坑再扣款**: 反过来的
    话两个并发的重投都会看到"还没扣过"。
    """
    with db.tx() as conn:
        row = conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO NOTHING RETURNING k",
            (key, str(int(time.time()))),
        ).fetchone()
    return row is not None


def _verify_report(ts: str, tenant: str, minutes: str, sig: str) -> bool:
    """验 GPU 侧的计费回报。

    签名覆盖**全部字段**, 尤其是分钟数 —— 只签租户的话, 任何拿到一个合法回报的
    人都能改大分钟数重放, 而那是直接从用户账上扣钱。
    """
    if not config.AVATAR_TOKEN_SECRET:
        return False
    try:
        if abs(time.time() - int(ts)) > TOKEN_TTL:
            return False
    except ValueError:
        return False
    want = hmac.new(
        config.AVATAR_TOKEN_SECRET.encode(),
        f"{ts}|{tenant}|{minutes}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(want, sig)


@router.get("/api/avatar/config")
async def avatar_config(user: dict = Depends(resolve_user)):
    """形象与音色清单。**代为转发**而不是让浏览器直连 GPU 节点。

    为什么不直连: 那样得把令牌暴露在前端能拿到的地方并允许跨域, 而令牌是能建立
    通话 (烧 GPU、扣积分) 的凭据。代转的话浏览器只跟我们说话, 令牌不出服务端。
    """
    if not config.AVATAR_TOKEN_SECRET:
        raise HTTPException(503, "avatar_not_configured")
    tok = sign_token(user["id"])
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{config.AVATAR_GPU_URL}/config", params={"token": tok})
        if r.status_code != 200:
            raise HTTPException(502, "avatar_upstream_error")
        return r.json()
    except httpx.HTTPError as e:
        log.warning("[avatar] config 取不到: %s", type(e).__name__)
        raise HTTPException(502, "avatar_unreachable") from None


@router.get("/api/avatar/bg.png")
async def avatar_bg(user: dict = Depends(resolve_user)):
    """静止背景。它是模型自渲染的中性帧合成图 —— 视频起播时不跳脸靠的就是它。"""
    if not config.AVATAR_TOKEN_SECRET:
        raise HTTPException(503, "avatar_not_configured")
    tok = sign_token(user["id"])
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{config.AVATAR_GPU_URL}/bg.png", params={"token": tok})
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "image/png"),
        )
    except httpx.HTTPError:
        raise HTTPException(502, "avatar_unreachable") from None


@router.post("/api/avatar/persons")
async def avatar_upload(request: Request, id: str = "", user: dict = Depends(resolve_user)):
    """上传形象。同样代转 —— 令牌不出服务端。"""
    if not config.AVATAR_TOKEN_SECRET:
        raise HTTPException(503, "avatar_not_configured")
    body = await request.body()
    if not body or len(body) > 20 * 1024 * 1024:
        return JSONResponse({"error": "图为空或超过 20MB"}, status_code=400)
    tok = sign_token(user["id"])
    try:
        async with httpx.AsyncClient(timeout=120.0) as c:
            r = await c.post(
                f"{config.AVATAR_GPU_URL}/persons", params={"token": tok, "id": id}, content=body
            )
        return JSONResponse(r.json(), status_code=r.status_code)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "avatar_unreachable") from None


@router.websocket("/api/avatar/ws")
async def avatar_ws(ws: WebSocket):
    """通话本身。双向转发到 GPU 节点。

    **令牌在这里重签, 不用前端传来的那个**: 前端手里那份是 /api/avatar/session
    给的, 它可能已经过期 (用户开着页面放了十分钟才点通话), 而过期的表现是
    "点了没反应" —— 最难查的那类。重签一次是零成本的。

    转发而不是让浏览器直连的另一个理由: 这样通话流量走我们的域, 前面压着
    forward_auth, 未登录的人连不上 —— 而 GPU 那边只认 HMAC, 谁拿到令牌谁能用。
    """
    import websockets

    await ws.accept()
    user = try_resolve_user(ws)
    if user is None:
        await ws.send_json({"type": "error", "message": "not_authenticated"})
        await ws.close()
        return
    if not config.AVATAR_TOKEN_SECRET:
        await ws.send_json({"type": "error", "message": "avatar_not_configured"})
        await ws.close()
        return

    q = dict(ws.query_params)
    q["token"] = sign_token(user["id"])  # 重签, 见上面的说明
    base = config.AVATAR_GPU_URL.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{base}/ws?" + urlencode(q)

    try:
        async with websockets.connect(url, max_size=None) as up:

            async def c2s() -> None:
                while True:
                    m = await ws.receive()
                    if m.get("type") == "websocket.disconnect":
                        break
                    if (b := m.get("bytes")) is not None:
                        await up.send(b)
                    elif (txt := m.get("text")) is not None:
                        await up.send(txt)

            async def s2c() -> None:
                async for data in up:
                    if isinstance(data, bytes):
                        await ws.send_bytes(data)
                    else:
                        await ws.send_text(data)

            done, pending = await asyncio.wait(
                [asyncio.create_task(c2s()), asyncio.create_task(s2c())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
    except Exception as e:  # noqa: BLE001
        log.warning("[avatar] ws 转发中断: %s", type(e).__name__)
    finally:
        try:
            await ws.close()
        except RuntimeError:
            pass
