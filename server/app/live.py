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
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, Response

from . import config, credits, db, model_catalog, rate_limit, security
from .accounts import resolve_user

router = APIRouter(prefix="/api/live", tags=["live"])
log = logging.getLogger("dhc.live")

#: 形象与音色的**固定搭配**。控制台只给这五个, 不再是两个各选各的下拉。
#:
#: 上游 /config 返回的 persons(5) 与 voices(7) 是两个**互不相干**的列表, 组合出 35 种,
#: 而设计过的只有这五种 —— 其余 30 种是意外, 最难受的是女性形象配上 yunjian/yunxi
#: 这类男声, 一开口就穿帮。旧控制台两个下拉还互不联动: 换了形象音色留在原地, 于是
#: 静默错配, 而界面毫无提示 (2026-09-10 创始人截图撞到: lin 的脸配着 xiaoya)。
#:
#: 这五对来自五个直播间 room.json 里当初存下的搭配, 不是我编的。
#: 一套搭配 = 形象 + 音色 + **人设**。三样绑死, 换一个就三样一起换。
#:
#: name/trait 沿用 1:1 通话页那五个 (avatar.js 的 PRESETS / i18n js.avatar.p.*) ——
#: 同一个形象在通话里叫"初雪 · 温柔", 在直播间也得叫这个, 否则观众看到的是两个人。
#: 在这之前直播这边只有形象和音色, 回评论用的是一条**没有身份**的通用提示词,
#: 换哪个形象她都是同一个没名字的人 (创始人 2026-09-10 提)。
#:
#: ⚠️ persona 是**追加**在 _REPLY 前面的, 不替换它: 不编造价格库存、被问就承认是
#:    数字人这些底线由 _REPLY 兜着, 而且排在后面(更靠近输出, 约束更强)。人设只管
#:    "怎么说话", 不管"能说什么"。
LIVE_PRESETS = [
    {"id": "default", "person": "source-v3-head", "voice": "xiaoya",
     "name": "初雪", "trait": "温柔",
     "persona": "你叫初雪。说话温柔、慢一点，句子短，语气软但不腻。"},
    {"id": "hao", "person": "hao", "voice": "yunxi",
     "name": "皓", "trait": "阳光",
     "persona": "你叫皓。说话阳光利落、有精神但不吵，偶尔带一点轻快的语气词。"},
    {"id": "chen", "person": "chen", "voice": "yunjian",
     "name": "晨", "trait": "沉稳",
     "persona": "你叫晨。说话沉稳、有分寸，不夸张也不起哄，像个可靠的老手。"},
    {"id": "yue", "person": "yue", "voice": "hsiaochen",
     "name": "悦", "trait": "干练",
     "persona": "你叫悦。说话干练直接，一句话说清楚，不绕弯子。"},
    {"id": "lin", "person": "lin", "voice": "xiaoxiao",
     "name": "林", "trait": "安静",
     "persona": "你叫林。说话安静、克制，不抢话，答得实在。"},
]


def preset_of(person: str, voice: str) -> dict:
    """把一对 (形象, 音色) 收敛到固定搭配里。

    先按整对精确匹配; 匹配不上就退而按形象找 (存量房间可能存着自由搭配的组合);
    再不行给第一个 —— **绝不返回 None**, 上层拿它填下拉, 空值会让整个选择器瞎掉。
    """
    for p in LIVE_PRESETS:
        if p["person"] == person and p["voice"] == voice:
            return p
    for p in LIVE_PRESETS:
        if p["person"] == person:
            return p
    return LIVE_PRESETS[0]


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
    _sample_rate(d)      # 顺手算产出速率, 掉出实时会记一条
    return JSONResponse(
        {
            "enabled": True,
            "live": bool(d.get("live")),
            "title": d.get("title", ""),
            "room": room,
            # 这一场是什么时候开的。播放器靠它认出"换了一场" —— 切形象/音色是在
            # 同一个请求里 stop+start, 客户端可能从头到尾都看到 live:true, 而播放
            # 列表已经被 rmtree 重建、MEDIA-SEQUENCE 退回 0。地址永远是同一个
            # index.m3u8, 所以不给这个信号就没法判断该不该拆掉重来。
            "since": float(d.get("since") or 0),
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
    _sample_rate(d)
    d["room"] = room
    d["hls"] = f"/api/live/hls/{room}/index.m3u8"
    # 实测产出速率。**这是控制台上唯一能回答"观众现在卡不卡"的数**: 低于 1.0 就是
    # 生产比消费慢, 缓冲开多大都会被抽干。
    # 2026-09-10 加: 之前控制台上完全看不出自己处在什么状态 —— 形象下拉只有名字,
    # 看不出哪个走云端 TTS(2.4x)哪个走自建(0.7x), 于是"我明明换了却还卡"这种误会
    # 没法自己排除。
    d["rate"] = _rate_now()
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


async def _compose_reply(comment: str, bill_to: str, device_id: str = "",
                         person: str = "") -> str:
    """让模型按 `_REPLY` 的口径回一句, 并把账记在 bill_to 头上。

    抽出来是因为**观众公屏和管理员插播走的是同一条路** —— 口径必须完全一致,
    否则"数字人会不会乱说话"这件事要审两遍。

    person 给了就把那套搭配的人设加在前面 (见 LIVE_PRESETS)。
    ⚠️ **加在前面, 不是替换**: _REPLY 排在后面兜底线(不编造价格库存、被问就承认是
       数字人), 人设只管"怎么说话"。顺序反了等于让人设去覆盖底线。
    """
    system = _REPLY
    if person:
        persona = (preset_of(person, "") or {}).get("persona") or ""
        if persona:
            system = persona + "\n" + _REPLY
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
                        {"role": "system", "content": system},
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
        # 也带上当前形象的人设 —— 管理员插播和观众公屏是同一个直播间的同一个人,
        # 口吻不一致观众听得出来。取不到房间配置就退回通用口径, 不能因此发不出去。
        try:
            cfg = await _gpu("GET", f"/rooms/{config.LIVE_ROOM}/status", config.LIVE_ROOM)
            person = str(cfg.get("person") or "")
        except Exception:
            person = ""
        spoken = await _compose_reply(text, user["id"], user.get("device_id", ""),
                                      person=person)

    await _gpu("POST", f"/rooms/{config.LIVE_ROOM}/interject", config.LIVE_ROOM, json={"text": spoken[:600]})
    return JSONResponse({"ok": True, "comment": text, "spoken": spoken[:600], "mode": mode})


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
_LAST_REPLY_AT = 0.0


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


#: ── 直播出问题时记一笔 ──────────────────────────────────────────────────────
#:
#: 2026-09-10 之前这件事**完全没有记录**。观众卡了几次没人知道(播放器检测到画面
#: 冻住只是自己跳一下, 不上报); 产出什么时候掉到实时以下也没人知道 —— 上游那个
#: `starved` 计数在产能不足时**恒为 0**, 因为它只在我们主动踩刹车导致队列空时才加,
#: 而产能不足时数字人自己就是瓶颈, 一刻不闲。于是每次报障都只能现场架探针去量,
#: 回头什么都查不到。这一段就是补这个洞。
_INCIDENT_KINDS = {
    "stall",     # 画面冻住 (观众侧)
    "waiting",   # 缓冲见底, 播放器在等数据 (观众侧) —— "播一会儿没声音"就是这个
    "fatal",     # 播放器致命错误 (观众侧)
    "rebuild",   # 换场重建 (观众侧)
    "autoplay",  # 自动播放被拒 (观众侧)
    "remuted",   # 被迫退回静音 (观众侧)
    "slow",      # 产出掉到实时以下 (产出侧)
    "recovered", # 产出恢复 (产出侧)
}


def _record(kind: str, side: str, *, secs: float = 0.0, lag: float = 0.0,
            detail: str = "", user_id: str = "") -> None:
    """记一条直播事件。**绝不能把主流程带崩** —— 观测坏了不该拖垮播放。"""
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO live_incidents "
                "(id, room, kind, side, user_id, secs, lag, detail, created) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (security.new_id("li_"), config.LIVE_ROOM, kind, side, user_id,
                 float(secs), float(lag), str(detail)[:200], time.time()),
            )
    except Exception as e:
        log.warning("记直播事件失败 (%s): %s", kind, e)


#: 产出速率采样。这一层本来就在轮询上游状态, 顺手拿 edge(直播边缘的视频秒数)算,
#: 所以**不用改 GPU 侧, 也就不用中断播出**。
_RATE: dict = {"pts": [], "slow": False}
_RATE_WINDOW = 150.0     # 采样保留多久
#: ⚠️ 跨度不够长不判定。节流让产出变成锯齿(句内出片, 句间空 5~8 秒), 短窗会把稳态
#: 1.000× 读成 0.79× 或 1.4× —— 2026-09-10 我就被这个骗过一次, 拿 45 秒的窗得出过
#: 相反的结论。
_RATE_MIN_SPAN = 60.0
_RATE_BAD = 0.95         # 低于这个算掉出实时
_RATE_OK = 0.99          # 回到这个才算恢复 (留迟滞, 免得在边界反复报)


def _rate_now() -> float:
    """当前窗口内的实测产出速率; 采样不够就回 0 (界面上显示成"测量中")。"""
    pts = _RATE["pts"]
    if len(pts) < 2:
        return 0.0
    span = pts[-1][0] - pts[0][0]
    if span < 20:
        return 0.0
    return round((pts[-1][1] - pts[0][1]) / span, 3)


def _sample_rate(d: dict) -> None:
    """从上游状态里顺手算产出速率, 掉出实时/恢复各记一条。

    只记**状态转换**, 不是每次采样都写库 —— 否则表会被正常运行时的噪声填满,
    而真正要回答的问题是"什么时候开始掉的"。
    """
    try:
        pts = _RATE["pts"]
        if not d.get("live"):
            pts.clear()
            return
        edge = float(d.get("edge") or 0)
        if edge <= 0:
            return                       # 上游还没升级, 给不出 edge
        now = time.time()
        if pts and edge < pts[-1][1]:    # 换场了: 时间轴从 0 重来, 之前的采样作废
            pts.clear()
            _RATE["slow"] = False
        pts.append((now, edge))
        while pts and now - pts[0][0] > _RATE_WINDOW:
            pts.pop(0)
        if len(pts) < 2:
            return
        span = pts[-1][0] - pts[0][0]
        if span < _RATE_MIN_SPAN:
            return
        rate = (pts[-1][1] - pts[0][1]) / span
        if not _RATE["slow"] and rate < _RATE_BAD:
            _RATE["slow"] = True
            _record("slow", "server", secs=span, detail=f"产出 {rate:.3f}x")
            log.warning("直播产出掉出实时: %.3fx (%.0f 秒窗)", rate, span)
        elif _RATE["slow"] and rate >= _RATE_OK:
            _RATE["slow"] = False
            _record("recovered", "server", secs=span, detail=f"产出 {rate:.3f}x")
            log.info("直播产出恢复: %.3fx", rate)
    except Exception as e:
        log.warning("产出速率采样失败: %s", e)


#: 字幕的服务端缓存。观众各自轮询的话, 一百个人就是每秒几十次打到 GPU 上 ——
#: 而所有人看的是同一场直播, 同一份内容。缓存两秒: 比切片时长 (1 秒) 长一点,
#: 短到察觉不出延迟。
_CAP_CACHE: dict[str, object] = {"at": 0.0, "data": None}
_CAP_TTL = 2.0


@router.get("/captions")
async def captions():
    """她刚才说了什么。**公开** —— 字幕是给观众看的。

    只回文本, 不回 kind 之外的任何东西: 这条路没有鉴权, 别把上游状态 (队列深度、
    错误、话术全文) 顺手带出去。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    now = time.time()
    if _CAP_CACHE["data"] is not None and now - float(_CAP_CACHE["at"]) < _CAP_TTL:
        return JSONResponse(_CAP_CACHE["data"])
    try:
        st = await _gpu("GET", f"/rooms/{config.LIVE_ROOM}/status", config.LIVE_ROOM)
    except HTTPException:
        # 上游够不着不该让字幕层报错 —— 观众看到的是画面还在、字幕停住, 那比
        # 整块红字好。
        return JSONResponse({"live": False, "lines": []})
    _sample_rate(st)     # 字幕是三秒一问的, 采样主要靠这里
    lines = [
        {
            "t": float(x.get("t") or 0),
            # 这一句的视频从整条流的第几秒开始。字幕靠它对齐 —— 墙钟那条依赖
            # "派单紧跟上一句结束", 而产出侧的墙钟节流打破了那个前提。
            # 上游给不出就是 None, 客户端会回落到墙钟算法。
            "vt": (float(x["vt"]) if x.get("vt") is not None else None),
            "kind": str(x.get("kind") or "script"),
            "text": str(x.get("text") or ""),
        }
        for x in (st.get("recent") or [])
        if str(x.get("text") or "").strip()
    ]
    # edge = 直播边缘此刻的视频秒数。是个单调计数, 不涉及队列深度/错误/话术全文,
    # 与上面"别把上游状态带出去"的约束不冲突 —— 而没有它就没法算观众播到哪一句。
    data = {
        "live": bool(st.get("live")),
        "edge": float(st.get("edge") or 0.0),
        "lines": lines[-12:],
    }
    _CAP_CACHE["at"], _CAP_CACHE["data"] = now, data
    return JSONResponse(data)


#: 同一个观众同一种事件多久才收第二条。播放器卡住时事件会连着来, 全收就是噪声。
_REPORT_COOLDOWN_S = 20


@router.post("/report")
async def report(body: dict, user: dict = Depends(resolve_user)):
    """播放器报一次它遇到的麻烦。登录才收 —— 直播页本来就要登录, 匿名收等于开个写口。

    只收固定几种 kind 和三个数, 不收自由文本以外的任何东西; detail 截断后入库。
    **失败不报错**: 观测坏了不该让播放页看到红字, 更不该让它重试。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    kind = str(body.get("kind", ""))[:32]
    if kind not in _INCIDENT_KINDS or kind in ("slow", "recovered"):
        # slow/recovered 是产出侧自己算的, 不接受客户端声称
        return JSONResponse({"ok": False, "skipped": "bad_kind"})
    if not rate_limit.allow(f"live-report:{user['id']}:{kind}", 1, _REPORT_COOLDOWN_S):
        return JSONResponse({"ok": False, "skipped": "too_fast"})

    def _num(key: str) -> float:
        try:
            v = float(body.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if 0 <= v < 86400 else 0.0

    _record(kind, "viewer", secs=_num("secs"), lag=_num("lag"),
            detail=str(body.get("detail", ""))[:200], user_id=user["id"])
    return JSONResponse({"ok": True})


@router.get("/incidents")
async def incidents(hours: float = 6.0, limit: int = 200,
                    user: dict = Depends(resolve_user)):
    """最近发生过什么。回答的是"什么时候开始出问题的"。

    不回 user_id —— 要的是"卡了多少次", 不是"谁卡了"。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    since = time.time() - max(0.1, min(float(hours or 6), 24 * 14)) * 3600
    rows = db.query(
        "SELECT kind, side, secs, lag, detail, created FROM live_incidents "
        "WHERE room=? AND created>? ORDER BY created DESC LIMIT ?",
        (config.LIVE_ROOM, since, max(1, min(int(limit or 200), 1000))),
    )
    items = [
        {
            "kind": r["kind"], "side": r["side"],
            "secs": float(r["secs"]), "lag": float(r["lag"]),
            "detail": r["detail"], "t": float(r["created"]),
        }
        for r in rows
    ]
    tally: dict = {}
    for it in items:
        tally[it["kind"]] = tally.get(it["kind"], 0) + 1
    return JSONResponse({"items": items, "tally": tally, "now": time.time()})


@router.get("/comments")
async def comments(since: float = 0.0, limit: int = 40):
    """公屏。**公开** —— 没登录也看得见, 否则路人打开直播间是一片死寂。

    只回没被藏起来的。since 是上一次拿到的最后一条的时间戳, 用来只取增量。
    """
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    rows = db.query(
        "SELECT id, nick, text, created, replied FROM live_comments "
        "WHERE room=? AND hidden=0 AND created>? ORDER BY created DESC LIMIT ?",
        (config.LIVE_ROOM, float(since or 0), max(1, min(int(limit or 40), 100))),
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
            (cid, config.LIVE_ROOM, user["id"], _nick(user), text, 0, 0, now),
        )

    replied = await _maybe_reply(cid, text)
    return JSONResponse({"ok": True, "id": cid, "nick": _nick(user), "t": now, "replied": replied})


async def _maybe_reply(cid: str, text: str) -> bool:
    """够不够格让她开口。飘屏是免费的, 开口不是 —— 三道闸都过了才回。

    任何一道没过都**不是错误**: 评论已经飘出去了, 她只是这一条没接话。所以这里
    一律吞掉异常, 绝不让"回答失败"变成"评论发不出去"。
    """
    global _LAST_REPLY_AT
    now = time.time()
    if now - _LAST_REPLY_AT < config.LIVE_REPLY_COOLDOWN_S:
        return False
    try:
        st = await _gpu("GET", f"/rooms/{config.LIVE_ROOM}/status", config.LIVE_ROOM)
    except HTTPException:
        return False
    if not st.get("live"):
        return False  # 没开播就没人听, 别白花钱
    if int(st.get("queued") or 0) >= config.LIVE_REPLY_MAX_QUEUE:
        return False  # 她已经排到几十秒开外了
    _LAST_REPLY_AT = now  # 先占位再去调模型 —— 慢的那几秒里别放第二条进来
    try:
        spoken = await _compose_reply(text, _bill_account(), person=str(st.get("person") or ""))
        hit = _claims(spoken)
        if hit:
            # 不重试: 同一个提示词刚说错过一次, 再抽一次多半还是错, 而每抽一次都
            # 在花钱, 观众还在等。直接换成安全的那句。
            log.warning("[live] 自动回评命中禁词 %r, 已换成安全兜底: %s", hit, spoken[:60])
            spoken = _SAFE_FALLBACK
        await _gpu("POST", f"/rooms/{config.LIVE_ROOM}/interject", config.LIVE_ROOM, json={"text": spoken})
    except Exception as e:  # noqa: BLE001
        _LAST_REPLY_AT = 0.0  # 没说成就把位子让出来
        log.warning("[live] 自动回评失败: %s", type(e).__name__)
        return False
    with db.tx() as conn:
        conn.execute("UPDATE live_comments SET replied=1 WHERE id=?", (cid,))
    return True


@router.post("/comment/{cid}/hide")
async def hide_comment(cid: str, user: dict = Depends(resolve_user)):
    """管理员把一条公屏藏起来。软删 —— 记录留着, UGC 的处置要留痕。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    _require_admin(user)
    with db.tx() as conn:
        conn.execute("UPDATE live_comments SET hidden=1 WHERE id=? AND room=?", (cid, config.LIVE_ROOM))
    return JSONResponse({"ok": True})
