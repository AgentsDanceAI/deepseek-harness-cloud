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

import asyncio
import logging
import re
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response

from . import config, credits, db, model_catalog, rate_limit, security
from .accounts import resolve_user

router = APIRouter(prefix="/api/live", tags=["live"])
log = logging.getLogger("dhc.live")

#: 切片可以缓存 (内容不可变), **播放列表绝对不行** —— 缓存住了播放器就永远看同一份
#: 切片列表, 表现是"画面卡在那儿"而没有任何一处报错。GPU 侧已经回了 no-store,
#: 这里再钉一次: 中间任何一层加了缓存都会复现这个 bug。
_M3U8 = "application/vnd.apple.mpegurl"


def _enabled() -> bool:
    return bool(config.LIVE_GPU_URL)


def rooms() -> list[str]:
    """有哪几间。顺序即页面顺序; 去重但保序。

    LIVE_ROOM 永远在列表里 —— 单间时代的配置不该因为升级就 404。
    """
    out: list[str] = []
    for r in (config.LIVE_ROOMS or "").split(","):
        r = r.strip()
        if r and r not in out:
            out.append(r)
    if config.LIVE_ROOM and config.LIVE_ROOM not in out:
        out.insert(0, config.LIVE_ROOM)
    return out or ["official"]


def _room(name: str | None) -> str:
    """把浏览器给的房间名换成一个**确实存在**的房间, 否则 404。

    这是唯一一处把外部字符串变成房间名的地方。放行任意字符串 = 任人拼出别的
    上游接口来 (那些接口是带令牌的)。
    """
    r = (name or "").strip() or config.LIVE_ROOM
    if r not in rooms():
        raise HTTPException(404, "no_such_room")
    return r


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
async def status(room: str = ""):
    """某一间的状态。未登录也给 —— 首页/目录要靠它决定卡片亮不亮。"""
    if not _enabled():
        return JSONResponse({"enabled": False, "live": False})
    try:
        room = _room(room)
    except HTTPException:
        return JSONResponse({"enabled": True, "live": False, "error": "no_such_room"})
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
    # 只放行名单里的房间 —— 这是一条把浏览器给的字符串拼进上游路径的路,
    # 不钉死就是任人拿我们当探测器去摸 GPU 节点上有什么。
    room = _room(room)
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
async def get_room(room: str = "", user: dict = Depends(resolve_user)):
    """某一间的配置: 话术、形象、音色、在播与否。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    room = _room(room)
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
    room = _room(body.get("room"))
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
async def act(action: str, room: str = "", user: dict = Depends(resolve_user)):
    """start / stop 某一间。"""
    # 实时形态下没有"渲染"这一步了 —— 话术存下去下一轮就当场生成。
    if action not in ("start", "stop"):
        raise HTTPException(404, "unknown_action")
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    room = _room(room)
    if action == "start":
        busy = [r for r in await _live_rooms() if r != room]
        if len(busy) >= config.LIVE_MAX_CONCURRENT:
            # **不排队, 直接拦。** 排队的语义是"等一会儿就轮到你", 但这里等不来 ——
            # 在播的那几间不会自己结束。而硬开的后果不是这一间卡, 是**所有人一起
            # 掉帧**: 三路已经吃掉那张卡 97% 的串行吞吐。
            # 把在播的房间名回给前端, 管理员才知道该去关哪一间。
            raise HTTPException(
                409,
                {"error": "too_many_live", "max": config.LIVE_MAX_CONCURRENT, "live": busy},
            )
    return JSONResponse(await _gpu("POST", f"/rooms/{room}/{action}", room))


#: 「哪几间在播」的缓存。开播那一步要先数一遍, 而每数一次就是 N 次上游调用;
#: 列表页也要用同一份。两秒足够 —— 开播/停播是人手点的, 不是每秒都在变。
_LIVE_CACHE: dict[str, object] = {"at": 0.0, "data": None}
_LIVE_TTL = 2.0


async def _room_status(room: str) -> dict:
    """一间的状态, 拿不到就当没开播 —— 上游抖一下不该让整页报错。"""
    try:
        return await _gpu("GET", f"/rooms/{room}/status", room)
    except HTTPException:
        return {"live": False, "error": "unreachable"}


async def _all_status() -> dict[str, dict]:
    now = time.time()
    cached = _LIVE_CACHE["data"]
    if cached is not None and now - float(_LIVE_CACHE["at"]) < _LIVE_TTL:
        return cached  # type: ignore[return-value]
    names = rooms()
    got = await asyncio.gather(*(_room_status(r) for r in names))
    data = dict(zip(names, got))
    _LIVE_CACHE["at"], _LIVE_CACHE["data"] = now, data
    return data


async def _live_rooms() -> list[str]:
    return [r for r, st in (await _all_status()).items() if st.get("live")]


@router.get("/rooms")
async def list_rooms():
    """有哪几间、各自在不在播。**公开** —— 开播了谁都能看, 列表当然也谁都能看。

    只回展示需要的字段: 名称、形象、在播与否。话术全文、队列深度、错误细节一律
    不带出去 (这条路没有鉴权)。
    """
    if not _enabled():
        return JSONResponse({"enabled": False, "rooms": [], "max": 0})
    st = await _all_status()
    return JSONResponse(
        {
            "enabled": True,
            "max": config.LIVE_MAX_CONCURRENT,
            "rooms": [
                {
                    "id": r,
                    "title": str((st.get(r) or {}).get("title") or ""),
                    "person": str((st.get(r) or {}).get("person") or ""),
                    "live": bool((st.get(r) or {}).get("live")),
                    "hls": f"/api/live/hls/{r}/index.m3u8",
                }
                for r in rooms()
            ],
        }
    )


#: 生成话术的系统提示。**每一句都会被读出来** —— 所以任何书面格式(编号、列表、
#: 星号、表情)都是噪音: 它们要么被念出来, 要么让断句变怪。长度也有讲究: TTS 每句
#: 有约 2 秒固定开销, 太短的句子极不划算 (一句 8 个字要等 2 秒), 太长又不好改。
_WRITER = (
    "你在给一场数字人直播写循环播放的口播话术。要求：\n"
    "1. 只输出话术本身，一行一句，不要编号、不要列表符号、不要星号、不要表情、不要标题。\n"
    "2. 每句 25 到 60 个字，是能一口气念完的完整句子。\n"
    "3. 口语，像真人在直播间说话，不要书面语，不要排比堆砌。\n"
    "4. 全篇会循环播放，所以最后一句之后要能自然接回第一句。\n"
    "5. 不要编造任何事实性内容：价格、优惠、券、福利、库存、发货时间、销量、排名、"
    "获奖、资质、功效承诺，以及「很多用户都说」「大家反馈」这类用户证言——这些不是"
    "文案技巧，说错了是虚假宣传。宁可只讲产品本身能做什么。\n"
    "6. 你是数字人主播，不要写任何暗示自己是真人的话。\n"
    "7. 8 到 12 句。"
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
    "你是一个**数字人主播**（AI 生成的虚拟主播），正在回观众的一条评论。要求：\n"
    "1. 一到两句话，口语，像真的在直播间开口回应，不要书面语。\n"
    "2. 只输出要说的话，不要引号、不要旁白、不要emoji、不要任何格式符号。\n"
    "3. **被问到是不是真人、是不是AI，一律大方承认自己是数字人**，可以说得轻松，"
    "但绝对不能说自己是真人——这是底线，不是风格问题。\n"
    "4. **绝对不要编造事实**：价格、优惠、券、福利、库存、发货时间、销量、排名、"
    "获奖、功效、资质，以及别人怎么说。这几样一个字都不许自己想。"
    "不知道就大方说这个稍后请客服回复你，别猜、别打包票、别说'放心拍'。\n"
    "5. 先回应他这句话本身，再自然接回直播的节奏。"
)


#: 自动回评里**不许出现**的字眼。提示词已经写了"不要编造价格/优惠/库存/功效",
#: 但 2026-09-09 上线第一条实测就翻车 —— 她自己加了句"今天直播间有专属优惠"。
#: 提示词管不住的东西, 在服务端拦。
#:
#: 这条线只管**自动回评**: 话术是人写完过目再保存的, 里面提活动是运营的决定;
#: 而自动回评没有人在环里, 说出去就是虚假宣传, 是要担责的那种错。
_FORBIDDEN = re.compile(
    r"优惠|折扣|打折|券|红包|秒杀|限时|包邮|买一送|赠品|免费送|中奖|抽奖"
    r"|库存|现货|发货|包退|假一赔|正品保证|保真"
    r"|销量第一|全网最低|最便宜|最低价|史低"
    r"|保证|承诺|包治|疗效|治疗|根治|药效"
)

#: 拦下来之后说什么。**不能沉默** —— 观众发了条评论, 她一声不吭比说错更像坏了。
#: 这句话是安全的: 只把问题交给客服, 不给任何承诺。
_SAFE_FALLBACK = "这个我这儿不敢替您打包票，稍后让客服同学给您准确答复哈。"


def _claims(text: str) -> str:
    """命中的那个词, 没有则空串。给日志用 —— 光知道"被拦了"排不了错。"""
    m = _FORBIDDEN.search(text or "")
    return m.group(0) if m else ""


async def _compose_reply(comment: str, bill_to: str, device_id: str = "") -> str:
    """让模型按 `_REPLY` 的口径回一句, 并把账记在 bill_to 头上。

    抽出来是因为**观众公屏和管理员插播走的是同一条路** —— 口径必须完全一致,
    否则"数字人会不会乱说话"这件事要审两遍。
    """
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
                        {"role": "user", "content": f"观众评论：{comment}"},
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
    if (uncached or output) and bill_to:
        credits.spend(
            bill_to,
            model_catalog.charge_credits(model_id, uncached, cache_read, output),
            kind="llm",
            model=model_id,
            device_id=device_id,
            uncached_input=uncached,
            cache_read=cache_read,
            output=output,
            request_id=f"live-reply-{uuid.uuid4().hex[:16]}",
        )
    return spoken[:600]


@router.post("/say")
async def say(body: dict, user: dict = Depends(resolve_user)):
    # room 从请求体里来 (与 put_room 一致), 而不是查询串 —— 这条是 POST。
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
    room = _room(body.get("room"))
    text = str(body.get("text", "")).strip()[:600]
    if not text:
        raise HTTPException(400, "empty_text")
    mode = "chat" if str(body.get("mode", "chat")) == "chat" else "echo"

    spoken = text
    if mode == "chat":
        spoken = await _compose_reply(text, user["id"], user.get("device_id", ""))

    await _gpu("POST", f"/rooms/{room}/interject", room, json={"text": spoken[:600]})
    return JSONResponse({"ok": True, "room": room, "comment": text, "spoken": spoken[:600], "mode": mode})


# ── 公屏 (2026-09-09) ────────────────────────────────────────────────────
# 老板拍板: 登录才能发, 每一条都自动回。
#
# "自动回每一条"在**任何登录用户都能触发**的前提下, 意味着三件事必须先想清楚:
#   1. 钱记谁头上 —— 观众只是发了句话, 没同意花钱; 记他头上是乱扣。记直播间运营
#      方 (LIVE_BILL_EMAIL / 第一个管理员) 头上, 一个地方看得见账。
#   2. 她的嘴是串行的 —— 一句念十来秒。十个人同时发, 话术两分钟一句都播不了。
#      所以上游队列深了就只飘屏不开口。
#   3. **观众永远拿不到 echo 模式** —— echo 是把文字原样念出去, 等于任何人都能
#      让她说任何话。观众只能走 chat, 由 `_REPLY` 那套口径过一道。
#: 按房间算冷却 —— 全局一份的话, 甲间刚回过一条, 乙间就得干等 12 秒, 而两间各有
#: 各的观众, 凭什么互相挡。
_LAST_REPLY_AT: dict[str, float] = {}


def _bill_account() -> str:
    """自动回评记账的用户 id。取不到就返回空 —— 那时**照样回答, 只是不记账**,
    因为"没配好计费"不该表现为"直播间不理人"。"""
    email = (config.LIVE_BILL_EMAIL or (config.ADMIN_EMAILS[0] if config.ADMIN_EMAILS else "")).strip()
    if not email:
        return ""
    row = db.query_one("SELECT id FROM users WHERE lower(email)=?", (email.lower(),))
    return str(row["id"]) if row else ""


def _nick(user: dict) -> str:
    """公屏上显示的名字。**不能露邮箱** —— 公屏是所有人可见的。

    ⚠️ 注册时 display_name 被填成了邮箱前缀 (accounts 建号那行)。所以"有
    display_name 就用它"是不够的: 没改过名的人, 昵称就是他的邮箱名, 而这一页
    上所有人都看得见。只有**自己改过**的名字才算数, 其余一律打码。
    """
    email = (user.get("email") or "").strip()
    head = email.split("@")[0]
    name = (user.get("display_name") or "").strip()
    if name and name != head:
        return name[:16]
    return (head[:2] + "***") if head else "观众"


#: 字幕的服务端缓存。观众各自轮询的话, 一百个人就是每秒几十次打到 GPU 上 ——
#: 而所有人看的是同一场直播, 同一份内容。缓存两秒: 比切片时长 (1 秒) 长一点,
#: 短到察觉不出延迟。
#: 按房间存 —— 全局一份的话, 甲间的字幕会被乙间的覆盖掉。
_CAP_CACHE: dict[str, dict] = {}
_CAP_TTL = 2.0


@router.get("/captions")
async def captions(room: str = ""):
    """她刚才说了什么。**公开** —— 字幕是给观众看的。

    只回文本, 不回 kind 之外的任何东西: 这条路没有鉴权, 别把上游状态 (队列深度、
    错误、话术全文) 顺手带出去。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    room = _room(room)
    now = time.time()
    hit = _CAP_CACHE.get(room)
    if hit is not None and now - float(hit["at"]) < _CAP_TTL:
        return JSONResponse(hit["data"])
    try:
        st = await _room_status(room)
        if st.get("error"):
            raise HTTPException(502, "upstream_unreachable")
    except HTTPException:
        # 上游够不着不该让字幕层报错 —— 观众看到的是画面还在、字幕停住, 那比
        # 整块红字好。
        return JSONResponse({"live": False, "lines": []})
    lines = [
        {"t": float(x.get("t") or 0), "kind": str(x.get("kind") or "script"), "text": str(x.get("text") or "")}
        for x in (st.get("recent") or [])
        if str(x.get("text") or "").strip()
    ]
    data = {"live": bool(st.get("live")), "lines": lines[-12:]}
    _CAP_CACHE[room] = {"at": now, "data": data}
    return JSONResponse(data)


@router.get("/comments")
async def comments(since: float = 0.0, limit: int = 40, room: str = ""):
    """公屏。**公开** —— 没登录也看得见, 否则路人打开直播间是一片死寂。

    只回没被藏起来的。since 是上一次拿到的最后一条的时间戳, 用来只取增量。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    room = _room(room)
    rows = db.query(
        "SELECT id, nick, text, created, replied FROM live_comments "
        "WHERE room=? AND hidden=0 AND created>? ORDER BY created DESC LIMIT ?",
        (room, float(since or 0), max(1, min(int(limit or 40), 100))),
    )
    items = [
        {
            "id": r["id"],
            "nick": r["nick"],
            "text": r["text"],
            "t": float(r["created"]),
            "replied": bool(r["replied"]),
        }
        for r in reversed(rows)
    ]
    return JSONResponse({"items": items, "now": time.time()})


@router.post("/comment")
async def comment(body: dict, user: dict = Depends(resolve_user)):
    """观众发一条公屏。登录才能发 —— 匿名发言追不到人, 出事时没有处置手段。

    返回**不等她说完**: 组织语言要几秒、排队再等十几秒, 让发言的人干等着不合理。
    飘屏是立刻的, 开不开口由下面几道闸决定。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    room = _room(body.get("room"))
    text = str(body.get("text", "")).strip()[: config.LIVE_COMMENT_MAX_LEN]
    if not text:
        raise HTTPException(400, "empty_text")
    if not rate_limit.allow(f"live-comment:{user['id']}", 1, config.LIVE_COMMENT_COOLDOWN_S):
        raise HTTPException(429, "too_fast")

    cid = security.new_id("lc_")
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO live_comments (id, room, user_id, nick, text, replied, hidden, created) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, room, user["id"], _nick(user), text, 0, 0, now),
        )

    replied = await _maybe_reply(cid, text, room)
    return JSONResponse({"ok": True, "id": cid, "room": room, "nick": _nick(user),
                         "t": now, "replied": replied})


async def _maybe_reply(cid: str, text: str, room: str) -> bool:
    """够不够格让她开口。飘屏是免费的, 开口不是 —— 三道闸都过了才回。

    任何一道没过都**不是错误**: 评论已经飘出去了, 她只是这一条没接话。所以这里
    一律吞掉异常, 绝不让"回答失败"变成"评论发不出去"。
    """
    now = time.time()
    if now - _LAST_REPLY_AT.get(room, 0.0) < config.LIVE_REPLY_COOLDOWN_S:
        return False
    try:
        st = await _room_status(room)
        if st.get("error"):
            return False
    except HTTPException:
        return False
    if not st.get("live"):
        return False  # 没开播就没人听, 别白花钱
    if int(st.get("queued") or 0) >= config.LIVE_REPLY_MAX_QUEUE:
        return False  # 她已经排到几十秒开外了
    _LAST_REPLY_AT[room] = now  # 先占位再去调模型 —— 慢的那几秒里别放第二条进来
    try:
        spoken = await _compose_reply(text, _bill_account())
        hit = _claims(spoken)
        if hit:
            # 不重试: 同一个提示词刚说错过一次, 再抽一次多半还是错, 而每抽一次都
            # 在花钱, 观众还在等。直接换成安全的那句。
            log.warning("[live] 自动回评命中禁词 %r, 已换成安全兜底: %s", hit, spoken[:60])
            spoken = _SAFE_FALLBACK
        await _gpu("POST", f"/rooms/{room}/interject", room, json={"text": spoken})
    except Exception as e:  # noqa: BLE001
        _LAST_REPLY_AT[room] = 0.0  # 没说成就把位子让出来
        log.warning("[live] 自动回评失败: %s", type(e).__name__)
        return False
    with db.tx() as conn:
        conn.execute("UPDATE live_comments SET replied=1 WHERE id=?", (cid,))
    return True


@router.post("/comment/{cid}/hide")
async def hide_comment(cid: str, room: str = "", user: dict = Depends(resolve_user)):
    """管理员把一条公屏藏起来。软删 —— 记录留着, UGC 的处置要留痕。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    room = _room(room)
    with db.tx() as conn:
        conn.execute("UPDATE live_comments SET hidden=1 WHERE id=? AND room=?", (cid, room))
    return JSONResponse({"ok": True})
