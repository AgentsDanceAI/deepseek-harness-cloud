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

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from . import config
from .accounts import resolve_user, try_resolve_user

router = APIRouter(prefix="/api/live", tags=["live"])
log = logging.getLogger("dhc.live")

#: 切片可以缓存 (内容不可变), **播放列表绝对不行** —— 缓存住了播放器就永远看同一份
#: 切片列表, 表现是"画面卡在那儿"而没有任何一处报错。GPU 侧已经回了 no-store,
#: 这里再钉一次: 中间任何一层加了缓存都会复现这个 bug。
_M3U8 = "application/vnd.apple.mpegurl"


def _enabled() -> bool:
    return bool(config.LIVE_GPU_URL)


def _my_room(user: dict) -> str:
    """每个用户一个直播间。前缀与数字人通话的租户一致 —— 那张卡上同时住着口袋专家
    和 DSH Cloud 两条线, 不加前缀两边的用户 id 可能撞上, 而撞了就是**看到/改到
    别人的直播间**。"""
    return "d-" + str(user["id"])


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
async def hls(room: str, name: str, request: Request):
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
    # 归属: 官方间人人可看 (它就是拿来展示的), 别人的间只有本人能看。
    # 房间名是 d-<用户id> —— 不是秘密, 猜得到; 所以隔离必须落在这里, 不能指望
    # "别人不知道房间名"。
    if room != config.LIVE_ROOM:
        me = try_resolve_user(request)
        if me is None or _my_room(me) != room:
            raise HTTPException(403, "not_your_room")
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
async def my_room(user: dict = Depends(resolve_user)):
    """我的直播间: 话术、形象、音色、逐句渲染状态、在播与否。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    room = _my_room(user)
    d = await _gpu("GET", f"/rooms/{room}/status", room)
    d["room"] = room
    d["hls"] = f"/api/live/hls/{room}/index.m3u8"
    return JSONResponse(d)


@router.put("/room")
async def put_my_room(body: dict, user: dict = Depends(resolve_user)):
    """存话术/形象/音色。**存完不会自动渲染** —— 渲染要占 GPU 几分钟, 用户改个
    错别字就整场重渲说不过去; 由他自己按"渲染"。"""
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    room = _my_room(user)
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
    """render / start / stop。"""
    if action not in ("render", "start", "stop"):
        raise HTTPException(404, "unknown_action")
    if not _enabled():
        raise HTTPException(404, "live_disabled")
    room = _my_room(user)
    return JSONResponse(await _gpu("POST", f"/rooms/{room}/{action}", room))
