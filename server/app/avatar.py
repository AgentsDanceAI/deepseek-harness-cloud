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

import hashlib
import hmac
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config, credits
from .accounts import resolve_user

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

    幂等靠 request_id (租户 + 分钟窗口): GPU 侧重试或我们这边超时重投时, 同一
    分钟不会被扣两次。
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
    credits.spend(
        user_id,
        amount,
        kind="llm",
        model="avatar:live",
        # 同一分钟窗口重投不会重复扣 —— GPU 侧网络抖动时会重试。
        request_id=f"avatar-{tenant}-{ts[:-2] if len(ts) > 2 else ts}",
    )
    log.info("[avatar] 计费 user=%s 分钟=%s 积分=%s", user_id, mins, amount)
    return {"ok": True, "charged": amount}


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
