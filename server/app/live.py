"""数字人直播间 —— 页面数据与 HLS 代转。

与数字人通话 (`avatar.py`) 同构: **没有每用户容器**, 算力在我们自己的 GPU 节点上,
这边只做转发。不同的是通话按分钟计费, 而直播的播出侧**不烧 GPU** —— 话术是离线
预渲染成片段的, 播出只是 ffmpeg 循环拼片段 (见 GPU 侧 docker/live/live_server.py)。

## 为什么 HLS 要代转, 而不是让浏览器直连 GPU 节点

两个都是"不代转就不工作"的硬理由, 不是洁癖:

  1. **CSP**。站点的 `default-src 'self'` 会挡掉跨源的 m3u8/ts 拉取 (hls.js 走的是
     fetch, 吃 connect-src)。要直连就得把 GPU 域名写进 connect-src, 等于给整页开
     一个跨源出口。同源代转则一条都不用改 —— 只需给 blob: 开 media-src (MediaSource
     的老坑, 见 security_headers.py 里数字人那条注释)。
  2. **CORS**。直连还要在 GPU 侧的 Caddy 上配 Access-Control-Allow-Origin, 又是一处
     和站点强耦合、改错了只在浏览器控制台报错的配置。

代价是视频字节流经这台机器 (每个观众约 2 Mbps)。观众多起来要换成 CDN 回源直取,
到时候连的是同一个 GPU 地址, 换的是路由不是协议。
"""

from __future__ import annotations

import logging
import re
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response

from . import config, credits, model_catalog
from .accounts import resolve_user

router = APIRouter(prefix="/api/live", tags=["live"])
log = logging.getLogger("dhc.live")

#: 切片可以缓存 (内容不可变), **播放列表绝对不行** —— 缓存住了播放器就永远看同一份
#: 切片列表, 表现是"画面卡在那儿"而没有任何一处报错。GPU 侧已经回了 no-store,
#: 这里再钉一次: 中间任何一层加了缓存都会复现这个 bug。
_M3U8 = "application/vnd.apple.mpegurl"


def _enabled() -> bool:
    return bool(config.LIVE_GPU_URL)


def _require_admin(user: dict) -> None:
    """控制台是管理员专用 (老板 2026-09-08 定)。

    只有**一套配置**: 全站共用官方直播间那一间。所以这里不需要"谁的房间"这个概念,
    只需要"你能不能改这一间" —— 而能改的只有管理员。
    """
    if not user.get("is_admin"):
        raise HTTPException(403, "admin_only")


async def _gpu(method: str, path: str, room: str, **kw):
    """带着这个房间自己的令牌调 GPU 侧。

    令牌的租户 == 房间名, 而房间名由**服务端**从登录态算出来 —— 所以浏览器无论
    传什么都只能操作自己那间。房间隔离全靠这一条, 别让房间名从请求体里进来。
    """
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.request(method, f"{config.LIVE_GPU_URL}{path}", params={"token": _sign(room)}, **kw)
    except httpx.HTTPError as e:
        # GPU 节点够不着是**常态之一** (它是别人的共享机, 还跟同事的排序管线挤一张卡)。
        # 不接的话异常一路冒到框架外, 用户看到 500 加一页栈 —— 而这只是"算力那头
        # 暂时不在"。/api/live/status 一开始就接了, 这条路当初漏了。
        log.warning("[live] 上游够不着 %s %s: %s", method, path, type(e).__name__)
        raise HTTPException(502, "upstream_unreachable") from None
    if r.status_code != 200:
        log.warning("[live] 上游 %s %s -> %s", method, path, r.status_code)
        raise HTTPException(502, "upstream")
    return r.json()


@router.get("/status")
async def status():
    """直播间状态。未登录也给 —— 首页/目录要靠它决定卡片亮不亮。"""
    if not _enabled():
        return JSONResponse({"enabled": False, "live": False})
    room = config.LIVE_ROOM
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{config.LIVE_GPU_URL}/rooms/{room}/status", params={"token": _sign(room)})
            r.raise_for_status()
            d = r.json()
    except Exception as e:
        # 直播间挂了不该让整页报错 —— 卡片显示"未开播"就够了。
        log.warning("取直播状态失败: %s", e)
        return JSONResponse({"enabled": True, "live": False, "error": "unreachable"})
    return JSONResponse(
        {
            "enabled": True,
            "live": bool(d.get("live")),
            "title": d.get("title", ""),
            "room": room,
            # 播放地址一律指向**我们自己**, 见模块头注释。
            "hls": f"/api/live/hls/{room}/index.m3u8",
        }
    )


def _sign(room: str) -> str:
    """与 GPU 侧同一把密钥、同一种令牌 (avatar 那套 v2 格式)。"""
    import hashlib
    import hmac
    import time

    ts = str(int(time.time()))
    sig = hmac.new(config.AVATAR_TOKEN_SECRET.encode(), f"{ts}|{room}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{room}.{sig}"


@router.get("/hls/{room}/{name}")
async def hls(room: str, name: str):
    """代转 HLS 播放列表与切片。

    只放行 .m3u8 / .ts 两种名字 —— 这是一条把浏览器给的字符串直接拼进上游 URL 的
    路径, 不挡就是任人拼出别的接口来 (上游那些接口是带令牌的)。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    if not (name.endswith(".m3u8") or name.endswith(".ts")):
        raise HTTPException(400, "bad_name")
    if not name.replace(".", "").replace("_", "").isalnum():
        raise HTTPException(400, "bad_name")
    if not room.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "bad_room")
    # 全站只有官方间这一间, 别的名字一律不给 —— 这是一条把浏览器给的字符串拼进
    # 上游路径的路, 不钉死就是任人拿我们当探测器去摸 GPU 节点上有什么。
    if room != config.LIVE_ROOM:
        raise HTTPException(404, "no_such_room")
    url = f"{config.LIVE_GPU_URL}/hls/{room}/{name}"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url)
    except Exception as e:
        log.warning("取切片失败 %s: %s", name, e)
        raise HTTPException(502, "upstream") from None
    if r.status_code != 200:
        raise HTTPException(r.status_code if r.status_code in (404, 403) else 502, "upstream")
    is_list = name.endswith(".m3u8")
    return Response(
        content=r.content,
        media_type=_M3U8 if is_list else "video/mp2t",
        headers={"Cache-Control": "no-store" if is_list else "public, max-age=60"},
    )


# ── 控制台: 只能操作自己那一间 ────────────────────────────────────────────
@router.get("/room")
async def get_room(user: dict = Depends(resolve_user)):
    """直播间配置: 话术、形象、音色、逐句渲染状态、在播与否。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    room = config.LIVE_ROOM
    d = await _gpu("GET", f"/rooms/{room}/status", room)
    d["room"] = room
    d["hls"] = f"/api/live/hls/{room}/index.m3u8"
    return JSONResponse(d)


@router.put("/room")
async def put_room(body: dict, user: dict = Depends(resolve_user)):
    """存话术/形象/音色。**存完不会自动渲染** —— 渲染要占 GPU 几分钟, 改个错别字
    就整场重渲说不过去; 由人自己按"渲染"。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    room = config.LIVE_ROOM
    payload = {}
    for k in ("person", "voice", "title"):
        if k in body:
            payload[k] = str(body[k])[:120]
    if "lines" in body:
        if not isinstance(body["lines"], list):
            raise HTTPException(400, "lines_must_be_list")
        payload["lines"] = [str(x)[:600] for x in body["lines"] if str(x).strip()][:200]
    return JSONResponse(await _gpu("PUT", f"/rooms/{room}", room, json=payload))


@router.post("/room/{action}")
async def act(action: str, user: dict = Depends(resolve_user)):
    """start / stop。"""
    # 实时形态下没有"渲染"这一步了 —— 话术存下去下一轮就当场生成。
    if action not in ("start", "stop"):
        raise HTTPException(404, "unknown_action")
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    room = config.LIVE_ROOM
    return JSONResponse(await _gpu("POST", f"/rooms/{room}/{action}", room))


#: 生成话术的系统提示。**每一句都会被读出来** —— 所以任何书面格式(编号、列表、
#: 星号、表情)都是噪音: 它们要么被念出来, 要么让断句变怪。长度也有讲究: TTS 每句
#: 有约 2 秒固定开销, 太短的句子极不划算 (一句 8 个字要等 2 秒), 太长又不好改。
_WRITER = (
    "你在给一场数字人直播写循环播放的口播话术。要求：\n"
    "1. 只输出话术本身，一行一句，不要编号、不要列表符号、不要星号、不要表情、不要标题。\n"
    "2. 每句 25 到 60 个字，是能一口气念完的完整句子。\n"
    "3. 口语，像真人在直播间说话，不要书面语，不要排比堆砌。\n"
    "4. 全篇会循环播放，所以最后一句之后要能自然接回第一句。\n"
    "5. 不要编造任何事实性内容：价格、优惠、库存、销量、排名、获奖、资质、功效承诺，"
    "以及「很多用户都说」「大家反馈」这类用户证言——这些不是文案技巧，说错了是虚假"
    "宣传。宁可只讲产品本身能做什么。\n"
    "6. 8 到 12 句。"
)

#: 模型有时仍会带上"1." "- " "**" 之类。**在服务端剥掉**, 别指望提示词能 100% 管住:
#: 漏一个的代价是数字人当众念出"星号星号"。
_JUNK = re.compile(r"^\s*(?:[-*•·]|\d+[.、)]|第[一二三四五六七八九十]+[句段])\s*")


@router.post("/generate")
async def generate(body: dict, user: dict = Depends(resolve_user)):
    """按直播间名称生成一版话术。**只返回，不保存** —— 生成的话术会被数字人当众
    念出去，得有人过一眼再存。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    if not config.UPSTREAM_BASE_URL or not config.UPSTREAM_API_KEY:
        raise HTTPException(503, "upstream_not_configured")
    topic = str(body.get("title", "")).strip()[:120]
    if not topic:
        raise HTTPException(400, "empty_title")

    model_id = model_catalog.default_model()
    entry = model_catalog.resolve(model_id) or {}
    payload = {
        "model": entry.get("upstream_model", model_id),
        "messages": [
            {"role": "system", "content": _WRITER},
            {"role": "user", "content": f"直播间名称：{topic}"},
        ],
        "max_tokens": 1200,
        "temperature": 0.8,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as c:
            r = await c.post(
                config.UPSTREAM_BASE_URL.rstrip("/") + "/chat/completions",
                json=payload,
                headers={
                    "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
                    "content-type": "application/json",
                },
            )
    except httpx.HTTPError as e:
        log.warning("[live] 生成话术: 上游够不着 %s", type(e).__name__)
        raise HTTPException(502, "upstream_unreachable") from None
    if r.status_code != 200:
        log.warning("[live] 生成话术: 上游 %s", r.status_code)
        raise HTTPException(502, "upstream")
    d = r.json()
    text = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()

    lines = []
    for raw in text.splitlines():
        line = _JUNK.sub("", raw).strip().strip('“”"')
        # 太短的丢掉: 多半是模型自己加的小标题或者"好的，以下是话术："这类开场白。
        if len(line) >= 12:
            lines.append(line[:600])
    if not lines:
        raise HTTPException(502, "empty_generation")

    # 照常计费 —— 这条路和别处一样在烧模型, 白送的话账就对不上。
    usage = d.get("usage") or {}
    cache_read = int(
        usage.get("prompt_cache_hit_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    uncached = max(0, int(usage.get("prompt_tokens") or 0) - cache_read)
    output = int(usage.get("completion_tokens") or 0)
    if uncached or output:
        credits.spend(
            user["id"],
            model_catalog.charge_credits(model_id, uncached, cache_read, output),
            kind="llm",
            model=model_id,
            device_id=user.get("device_id", ""),
            uncached_input=uncached,
            cache_read=cache_read,
            output=output,
            request_id=f"live-gen-{uuid.uuid4().hex[:16]}",
        )
    return JSONResponse({"lines": lines[:20]})


#: 回评论时的口径。与写话术那份共享同一条底线 —— **不编造事实**。
#: 直播间里回评论比写话术更容易翻车: 观众问的往往就是价格、库存、效果这些具体的东西,
#: 而模型天生倾向于"给个答案"。所以这里把"不知道就说去问客服"写成明确指令。
_REPLY = (
    "你是直播间的主播，正在回观众的一条评论。要求：\n"
    "1. 一到两句话，口语，像真的在直播间开口回应，不要书面语。\n"
    "2. 只输出要说的话，不要引号、不要旁白、不要emoji、不要任何格式符号。\n"
    "3. **绝对不要编造事实**：价格、优惠、库存、发货时间、销量、排名、功效、"
    "资质，以及别人怎么说。不知道就大方说这个稍后请客服回复你，别猜。\n"
    "4. 先回应他这句话本身，再自然接回直播的节奏。"
)


@router.post("/say")
async def say(body: dict, user: dict = Depends(resolve_user)):
    """把一条评论送进直播间。

    两种模式, 与 LiveTalking 的文本驱动同形:
      · echo —— 原样念出去 (主播自己想插一句话时用);
      · chat —— 先让模型答, 再把答案念出去 (回观众评论)。

    **插播是排在当前句之后播的, 不打断当前句** —— 打断会把正在生成的那句撕成半截,
    整条流会断。所以从点发送到听见, 约十秒。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    text = str(body.get("text", "")).strip()[:600]
    if not text:
        raise HTTPException(400, "empty_text")
    mode = "chat" if str(body.get("mode", "chat")) == "chat" else "echo"

    spoken = text
    if mode == "chat":
        if not config.UPSTREAM_BASE_URL or not config.UPSTREAM_API_KEY:
            raise HTTPException(503, "upstream_not_configured")
        model_id = model_catalog.default_model()
        entry = model_catalog.resolve(model_id) or {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as c:
                r = await c.post(
                    config.UPSTREAM_BASE_URL.rstrip("/") + "/chat/completions",
                    json={
                        "model": entry.get("upstream_model", model_id),
                        "messages": [
                            {"role": "system", "content": _REPLY},
                            {"role": "user", "content": f"观众评论：{text}"},
                        ],
                        # 一句话。放开了她会说成一段稿子, 而那要念上一分钟, 后面的
                        # 评论全堵住。
                        "max_tokens": 160,
                        "temperature": 0.7,
                    },
                    headers={
                        "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
                        "content-type": "application/json",
                    },
                )
        except httpx.HTTPError:
            raise HTTPException(502, "upstream_unreachable") from None
        if r.status_code != 200:
            raise HTTPException(502, "upstream")
        d = r.json()
        spoken = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        spoken = _JUNK.sub("", spoken).strip().strip('“”"')
        if not spoken:
            raise HTTPException(502, "empty_generation")
        usage = d.get("usage") or {}
        cache_read = int(
            usage.get("prompt_cache_hit_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
        uncached = max(0, int(usage.get("prompt_tokens") or 0) - cache_read)
        output = int(usage.get("completion_tokens") or 0)
        if uncached or output:
            credits.spend(
                user["id"],
                model_catalog.charge_credits(model_id, uncached, cache_read, output),
                kind="llm",
                model=model_id,
                device_id=user.get("device_id", ""),
                uncached_input=uncached,
                cache_read=cache_read,
                output=output,
                request_id=f"live-reply-{uuid.uuid4().hex[:16]}",
            )

    await _gpu("POST", f"/rooms/{config.LIVE_ROOM}/interject", config.LIVE_ROOM, json={"text": spoken[:600]})
    return JSONResponse({"ok": True, "comment": text, "spoken": spoken[:600], "mode": mode})
