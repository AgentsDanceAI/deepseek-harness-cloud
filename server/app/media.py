"""视频生成 —— 异步作业制的计费与代理。

和 /llm/v1/chat/completions 有两处本质不同, 决定了它不能复用聊天那条路:

1. **上游不返回 usage。** 聊天按 token 结算 (model_catalog.charge_credits),
   而视频端点的 `usage` 字段实测恒为 null —— 只能按件计价, 价目见
   config/video_models.json。图像端点**有** usage, 所以图像仍走聊天那条路,
   不在本模块。

2. **一次请求打不完。** 视频要几十秒到几分钟, 用户会关页面、会换设备, 所以
   作业状态必须落库 (video_jobs 表), 由客户端轮询。

## 为什么是提交时预扣

作业一旦发给上游, 钱就已经花出去了 —— 我们无法撤回, 而用户完全可以提交完就
关掉页面再也不来查。等完成再结算等于给了一个白嫖口子, 且 workspace 那套空闲
回收管不到已经发出去的上游作业。

所以顺序是: **查余额 → 提交上游 → 提交成功才扣费**。提交失败 (参数非法、上游
拒绝) 根本不产生扣费, 因此那条路上不需要退款。只有"提交成功但生成失败"才退,
由 video_jobs.refunded 保证幂等 —— 轮询是客户端驱动的, 同一个失败作业会被查
很多次, 不加这个标记会退很多次钱。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from . import config, credits, db, model_catalog, plans, security
from .accounts import resolve_user
from .http_limits import read_limited_body

# 必须挂在 "dhc" 这一支下面: main.py 只配置了这棵树, 用 __name__ ("app.media")
# 的话本模块所有日志被静默丢弃 —— 退款记录、兜底循环的异常, 全都写进虚空。
log = logging.getLogger("dhc.media")
router = APIRouter(prefix="/llm")

_PRICES_PATH = Path(__file__).resolve().parents[1] / "config" / "media_models.json"
_prices_cache: dict | None = None
_image_cache: dict | None = None


def _load(section: str) -> dict:
    try:
        raw = json.loads(_PRICES_PATH.read_text(encoding="utf-8"))
        return {m["id"]: m for m in raw.get(section, [])}
    except (OSError, ValueError, KeyError):
        log.exception("media_models.json 读不了, %s 功能关闭", section)
        return {}


def _catalog() -> dict:
    """{model_id: {name, credits_per_second: {resolution: credits|None}}}"""
    global _prices_cache
    if _prices_cache is None:
        _prices_cache = _load("video")
    return _prices_cache


def _image_catalog() -> dict:
    """{model_id: {name, credits_per_image: credits|None}}"""
    global _image_cache
    if _image_cache is None:
        _image_cache = _load("image")
    return _image_cache


def _image_priced(m: dict) -> bool:
    """有没有定价。按张分档时, 全档都是 null 才算没定价 —— 直接判 dict 真假会把
    {"1k": null, "2k": null} 当成"已定价", 于是它露在下拉里, 点了按 1 积分卖。"""
    if m.get("usd_per_1m_image_tokens"):
        return True
    per_item = m.get("credits_per_image")
    if isinstance(per_item, dict):
        return any(per_item.values())
    return bool(per_item)


def offered() -> dict:
    """当前真正在售的媒体模型。未定价的不列 —— 露出来只会让人选了报 404。

    给 ComfyUI 的自有节点用: 它在 INPUT_TYPES() 里拉这份清单, 把"模型"做成
    下拉而不是自由文本框。用户不该需要背 doubao-seedance-2-0-mini-260615。
    """
    video = [
        {
            "id": m["id"],
            "name": m.get("name") or m["id"],
            # 按**数值**排, 不按字典序: 字典序会把 1080p 排到 480p 前面, 而下拉
            # 默认选中第一项 —— 那意味着谁第一次点运行都是最贵的那档。
            "resolutions": sorted(
                (r for r, v in (m.get("credits_per_second") or {}).items() if v),
                key=lambda r: int("".join(c for c in r if c.isdigit()) or 0),
            ),
        }
        for m in _catalog().values()
        if any((m.get("credits_per_second") or {}).values()) and provider_available(provider_of(m))
    ]
    image = [
        {"id": m["id"], "name": m.get("name") or m["id"]}
        for m in _image_catalog().values()
        if _image_priced(m) and provider_available(provider_of(m))
    ]
    return {"video": video, "image": image}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": code, "message": message}}, status_code=status)


def _gate(user: dict) -> JSONResponse | None:
    """灰度闸。价格是手填且未经真实账单核对的, 在核对前只放管理员进来 ——
    定价填错时全量开放意味着按错误的价格真金白银地卖, 那种错只能靠对账收场。"""
    if config.MEDIA_ADMIN_ONLY and not user.get("is_admin"):
        return _error(403, "not_available", "媒体生成正在灰度中，暂未对全部账号开放。")
    return None


def price_of(model: str, resolution: str) -> int | None:
    """这一档卖多少积分/秒。None = 未定价 = 不售卖。"""
    entry = _catalog().get(model)
    if not entry:
        return None
    value = (entry.get("credits_per_second") or {}).get(resolution)
    return None if value is None else int(value)


def quote(model: str, resolution: str, duration: int) -> int | None:
    per_second = price_of(model, resolution)
    if per_second is None:
        return None
    return max(1, math.ceil(per_second * max(1, duration)))


# 1K/2K 的分档按**像素面积**, 不按边长 —— 厂商就是这么算的 (百炼: 面积
# <= 2,250,000 算 1K)。按边长判会把 2560x800 这种宽幅错判成 2K。
_IMAGE_1K_MAX_AREA = 2_250_000


def _image_tier(size: str) -> str:
    """把 "1328*1328" / "1024x1024" 折成 1k / 2k 档。认不出来时按贵的算。"""
    nums = [int(n) for n in str(size or "").replace("x", "*").split("*") if n.strip().isdigit()]
    if len(nums) != 2:
        return "2k"
    return "1k" if nums[0] * nums[1] <= _IMAGE_1K_MAX_AREA else "2k"


def image_credits(entry: dict, usage: dict | None, size: str = "") -> int:
    """按 usage 里的真实 token 数计价。

    图像**不按张收**: 同一个模型低画质与高画质相差 35 倍
    (gpt-image-2 一张 1024²: $0.006 vs $0.211), 按张收要么坑用户要么亏钱, 而
    token 数会如实反映画质与尺寸。口径与聊天一致 —— 同一个 CREDITS_PER_USD
    与 MODEL_PRICE_MARKUP, 所以两条线的毛利率天然一致。

    上游没给 usage 时回落到 fallback_credits: 宁可贵也不能免费。
    """
    # 百炼那侧的同步生图**不返回 token 用量**, 只能按张。千面有 usage, 按 token ——
    # 后者更准 (同一模型高低画质差 35 倍), 所以有 token 单价就优先用它。
    if not entry.get("usd_per_1m_image_tokens"):
        per_item = entry.get("credits_per_image")
        # 按张也可能分档: qwen-image-3.0-pro 的 1K 与 2K 差整整一倍 (¥0.25 / ¥0.5)。
        # 一口价要么按 2K 收 (1K 的人被多收一倍), 要么按 1K 收 (2K 亏本)。
        if isinstance(per_item, dict):
            per_item = per_item.get(_image_tier(size))
        return max(1, int(per_item or entry.get("fallback_credits") or 0))
    out = int((usage or {}).get("output_tokens") or 0)
    text_in = int(((usage or {}).get("input_tokens_details") or {}).get("text_tokens") or 0)
    if not out:
        return max(1, int(entry.get("fallback_credits") or 0))
    usd = (
        out * float(entry.get("usd_per_1m_image_tokens") or 0)
        + text_in * float(entry.get("usd_per_1m_text_input_tokens") or 0)
    ) / 1_000_000
    return max(1, math.ceil(usd * model_catalog.CREDITS_PER_USD * config.MODEL_PRICE_MARKUP))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(config.UPSTREAM_TIMEOUT_S, connect=15.0))


def _auth_headers() -> dict:
    return {
        "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
        "content-type": "application/json",
    }


# --- 上游适配 ------------------------------------------------------------------
# 两家上游的形状完全不同, 差异集中在三处:
#
#              千面 (经网关)                 百炼 (直连)
#   分辨率     "480p"                        "832*480"  (宽*高)
#   状态词     PROCESSING/SUCCESS/FAIL       PENDING/RUNNING/SUCCEEDED/FAILED/CANCELED
#   产物       data.url                      output.video_url
#   视频用量   usage: null (只能按件计价)     usage:{video_duration,video_ratio,video_count}
#
# 图像那边差得更远: 千面是 OpenAI 风格 /images/generations 返回 b64 + token 用量;
# 百炼要走原生 multimodal-generation, 返回一个 OSS URL, 没有 token 用量。

QIANMIAN = "qianmian"
BAILIAN = "bailian"

# 百炼要的是像素尺寸。这几个是各分辨率的标准宽高 (实测 wanx2.1-t2v-turbo 接受)。
# 百炼的视频参数有**两代写法**, 用错那代不会报错 —— 字段被静默忽略, 按模型默认
# 值出片。2026-08-28 实测: 给 wan2.7-t2v 发 size="1280*720", 它照样按 1080P 出,
# usage 回 SR=1080。我们按 720p 收 10 积分/秒, 成本却是 1080P 的 $0.1434/秒 ——
# 每单亏七成, 而且没有任何报错。所以哪个模型用哪代必须写在目录里, 不靠猜。
#
#   size            wanx2.1 / wan2.6 一代: parameters.size = "1280*720"
#   resolution_ratio wan2.7 / wan3.0 一代: parameters.resolution="720P" + ratio="16:9"
#
# 后者是唯一能出**竖屏**的写法 —— 前者那张表全是 16:9, 用户选 9:16 也拿不到。
_BAILIAN_SIZE = {"480p": "832*480", "720p": "1280*720", "1080p": "1920*1080"}
_BAILIAN_RESOLUTION = {"480p": "480P", "720p": "720P", "1080p": "1080P"}
_RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "adaptive")
_PARAM_STYLES = ("size", "resolution_ratio")
# 素材怎么给。wan2.5-2.7 只有一张首帧图 (input.img_url); wan3.0 改成一个数组,
# 能带首帧、尾帧、参考图、参考视频 —— 把它压成一张首帧, 等于把用户的参考素材
# 悄悄丢掉, 他付了钱却拿到一条无视素材的视频, 比直接报错更糟。
_INPUT_STYLES = ("img_url", "media")
# 一次最多带几件素材, 以及单个 URL 的长度上限 (data: URI 会很长)。
_MEDIA_MAX_ITEMS = 8
_MEDIA_MAX_URL = 8 * 1024 * 1024


def video_param_style(model: str) -> str:
    """这个模型该用哪代参数写法。目录没写就按老写法 —— 老写法是 wanx2.1 那批。"""
    style = str((_catalog().get(model) or {}).get("video_params") or "size")
    return style if style in _PARAM_STYLES else "size"


def video_input_style(model: str) -> str:
    """素材是给一张首帧 (img_url) 还是给一个数组 (media)。"""
    style = str((_catalog().get(model) or {}).get("video_input") or "img_url")
    return style if style in _INPUT_STYLES else "img_url"
_BAILIAN_TERMINAL = {"SUCCEEDED": "succeeded", "FAILED": "failed", "CANCELED": "failed", "UNKNOWN": "failed"}


def provider_of(entry: dict | None) -> str:
    return str((entry or {}).get("provider") or QIANMIAN)


def provider_available(provider: str) -> bool:
    """这个上游现在能不能用 —— 只看凭据配没配。

    ⚠️ BAILIAN_NATIVE_BASE 必须是**业务空间专属域名**。没配就不可用, **绝不回落
    到公共 dashscope.aliyuncs.com**: 公共域名一样能通、结果也一样, 但预付套餐不
    抵扣、走按量计费, 且没有任何报错提示 (AgentsDance 2026-08-12 踩过)。
    """
    if provider != BAILIAN:
        return True
    return bool(config.BAILIAN_NATIVE_BASE and config.BAILIAN_API_KEY)


def _bailian_headers(async_mode: bool = False) -> dict:
    h = {
        "authorization": f"Bearer {config.BAILIAN_API_KEY}",
        "content-type": "application/json",
    }
    if async_mode:
        h["X-DashScope-Async"] = "enable"
    return h


async def submit_video(
    provider: str, model: str, prompt: str, resolution: str, duration: int,
    image_url: str = "", ratio: str = "", media: list | None = None,
) -> tuple[str, dict | None, int]:
    """向上游下单。返回 (task_id, 错误报文, 错误码) —— 成功时后两者为 None/0。"""
    if provider == BAILIAN:
        if video_param_style(model) == "resolution_ratio":
            params: dict = {"resolution": _BAILIAN_RESOLUTION.get(resolution), "ratio": ratio or "16:9"}
        else:
            params = {"size": _BAILIAN_SIZE.get(resolution)}
        params["duration"] = duration or None
        payload: dict = {
            "model": model,
            "input": {"prompt": prompt},
            "parameters": {k: v for k, v in params.items() if v},
        }
        if video_input_style(model) == "media":
            if media:
                payload["input"]["media"] = media
        elif image_url:
            payload["input"]["img_url"] = image_url
        url = f"{config.BAILIAN_NATIVE_BASE}/services/aigc/video-generation/video-synthesis"
        async with _client() as client:
            up = await client.post(url, headers=_bailian_headers(async_mode=True), json=payload)
        if up.status_code >= 400:
            return "", _safe_json(up), up.status_code
        task = ((up.json() or {}).get("output") or {}).get("task_id")
        if not task:
            return "", {"error": {"message": "百炼没有返回 task_id"}}, 502
        return str(task), None, 0

    payload = {"model": model, "prompt": prompt, "resolution": resolution, "duration": duration}
    if image_url:
        payload["image_url"] = image_url
    url = config.UPSTREAM_BASE_URL.rstrip("/") + "/video/generations"
    async with _client() as client:
        up = await client.post(url, headers=_auth_headers(), json=payload)
    if up.status_code >= 400:
        return "", _safe_json(up), up.status_code
    try:
        return str(up.json()["task_id"]), None, 0
    except (ValueError, KeyError):
        return "", {"error": {"message": "上游没有返回 task id"}}, 502


async def poll_video(provider: str, task_id: str) -> dict:
    """问一次上游。返回 {status: processing|succeeded|failed, url, error}。

    **上游抖动一律返回 processing** —— 把网络错误当成失败会白退钱。
    """
    try:
        if provider == BAILIAN:
            url = f"{config.BAILIAN_NATIVE_BASE}/tasks/{task_id}"
            async with _client() as client:
                up = await client.get(url, headers=_bailian_headers())
            out = (up.json() or {}).get("output") or {}
            state = _BAILIAN_TERMINAL.get(str(out.get("task_status") or "").upper())
            if state == "succeeded":
                return {"status": "succeeded", "url": str(out.get("video_url") or ""), "error": ""}
            if state == "failed":
                msg = str(out.get("message") or out.get("code") or "生成失败")
                return {"status": "failed", "url": "", "error": msg[:500]}
            return {"status": "processing", "url": "", "error": ""}

        url = config.UPSTREAM_BASE_URL.rstrip("/") + f"/video/generations/{task_id}"
        async with _client() as client:
            up = await client.get(url, headers=_auth_headers())
        data = (up.json() or {}).get("data") or {}
        state = str(data.get("status") or "").lower()
        if state == "succeeded":
            return {"status": "succeeded", "url": str(data.get("url") or ""), "error": ""}
        if state in ("failed", "error", "cancelled"):
            raw = data.get("error")
            msg = str(raw if not isinstance(raw, dict) else json.dumps(raw, ensure_ascii=False))
            return {"status": "failed", "url": "", "error": msg[:500]}
        return {"status": "processing", "url": "", "error": ""}
    except (httpx.HTTPError, ValueError):
        return {"status": "processing", "url": "", "error": ""}


async def gen_image(
    provider: str, model: str, prompt: str, n: int, extra: dict
) -> tuple[dict | None, dict | None, int]:
    """出图。返回 (结果, 错误报文, 错误码)。结果统一成 OpenAI 那套形状。"""
    if provider == BAILIAN:
        # 百炼的图像走原生 multimodal-generation, 同步返回一个 OSS URL, **没有
        # token 用量** —— 所以这一侧只能按张计价。
        url = f"{config.BAILIAN_NATIVE_BASE}/services/aigc/multimodal-generation/generation"
        content: list = [{"text": prompt}]
        if extra.get("image_url"):
            content.insert(0, {"image": extra["image_url"]})
        payload = {"model": model, "input": {"messages": [{"role": "user", "content": content}]}}
        # size 必须转达上去。丢掉它的话我们按 2K 档收钱, 百炼却按默认尺寸出图 ——
        # 用户多付了钱, 拿到的还是小图, 而且两边都不报错。
        # 百炼写 "1328*1328", OpenAI 那套写 "1024x1024", 统一成前者。
        params = {}
        if extra.get("size"):
            params["size"] = str(extra["size"]).replace("x", "*")
        if n > 1:
            params["n"] = n
        if params:
            payload["parameters"] = params
        async with _client() as client:
            up = await client.post(url, headers=_bailian_headers(), json=payload, timeout=300.0)
        if up.status_code >= 400:
            return None, _safe_json(up), up.status_code
        out = (up.json() or {}).get("output") or {}
        images = [
            part["image"]
            for choice in (out.get("choices") or [])
            for part in ((choice.get("message") or {}).get("content") or [])
            if isinstance(part, dict) and part.get("image")
        ]
        if not images:
            return None, {"error": {"message": "百炼没有返回图片"}}, 502
        return {"created": 0, "data": [{"url": u} for u in images], "usage": None}, None, 0

    payload = {"model": model, "prompt": prompt, "n": n}
    for key in ("size", "quality", "background", "output_format", "image_url"):
        if extra.get(key) is not None:
            payload[key] = extra[key]
    url = config.UPSTREAM_BASE_URL.rstrip("/") + "/images/generations"
    async with _client() as client:
        up = await client.post(url, headers=_auth_headers(), json=payload, timeout=300.0)
    if up.status_code >= 400:
        return None, _safe_json(up), up.status_code
    try:
        return up.json(), None, 0
    except ValueError:
        return None, {"error": {"message": "上游返回的不是 JSON"}}, 502


def _safe_json(resp) -> dict:  # noqa: ANN001
    try:
        return resp.json()
    except ValueError:
        return {"error": {"message": resp.text[:400]}}


def _job_row(job_id: str, user_id: str):
    return db.query_one("SELECT * FROM video_jobs WHERE id = ? AND user_id = ?", (job_id, user_id))


def _refund_once(job: dict) -> None:
    """失败作业退款, 幂等。

    先把 refunded 置 1 并要求它原本为 0, 拿到"这一次更新真的改到了行"才发放 ——
    先发钱再标记的话, 两个并发轮询会各发一次。
    """
    amount = int(job["credits"] or 0)
    if amount <= 0 or int(job["refunded"] or 0):
        return
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE video_jobs SET refunded = 1, updated = ? WHERE id = ? AND refunded = 0",
            (time.time(), job["id"]),
        )
        changed = getattr(cur, "rowcount", 0)
    if changed != 1:
        return  # 另一个请求已经退过了
    credits.grant(job["user_id"], amount, config.VIDEO_REFUND_TTL_S, kind="refund", ref=job["id"])
    log.info("视频作业 %s 生成失败, 退回 %d 积分", job["id"], amount)


@router.post("/v1/videos/generations")
async def create_video(request: Request, user: dict = Depends(resolve_user)):
    if not config.UPSTREAM_API_KEY:
        return _error(503, "upstream_unconfigured", "Video generation is not configured.")
    gated = _gate(user)
    if gated is not None:
        return gated

    blocked = plans.check_run_blocked(user["id"])
    if blocked:
        return _error(402, "insufficient_credits", "余额不足或已达额度上限，无法提交新作业。")

    try:
        raw = await read_limited_body(
            request,
            max_bytes=config.GATEWAY_BODY_MAX_BYTES,
            timeout_s=config.REQUEST_BODY_TIMEOUT_S,
        )
        body = json.loads(raw)
    except json.JSONDecodeError:
        return _error(400, "invalid_request_error", "Body must be JSON.")

    model = str(body.get("model") or "")
    prompt = str(body.get("prompt") or "")
    resolution = str(body.get("resolution") or config.VIDEO_DEFAULT_RESOLUTION)
    ratio = str(body.get("ratio") or "")
    if ratio and ratio not in _RATIOS:
        return _error(400, "invalid_request_error", f"ratio must be one of {', '.join(_RATIOS)}.")
    media = body.get("media") or []
    if not isinstance(media, list) or len(media) > _MEDIA_MAX_ITEMS:
        return _error(400, "invalid_request_error",
                      f"media must be a list of at most {_MEDIA_MAX_ITEMS} items.")
    for item in media:
        url_ = str((item or {}).get("url") or "") if isinstance(item, dict) else ""
        if not url_ or len(url_) > _MEDIA_MAX_URL:
            return _error(400, "invalid_request_error", "each media item needs a url.")
        if not url_.startswith(("http://", "https://", "data:")):
            return _error(400, "invalid_request_error", "media url must be http(s) or a data URI.")
    try:
        duration = int(body.get("duration") or config.VIDEO_DEFAULT_DURATION)
    except (TypeError, ValueError):
        return _error(400, "invalid_request_error", "duration must be an integer.")
    # 时长必须是正整数。quote() 里的 max(1, duration) 挡住了负积分, 但挡不住
    # **少收钱**: duration=-1 (ComfyUI 的 auto 档) 会按 1 秒计价, 而上游可能自动
    # 生成到 30 秒。宁可让客户端明确选一个时长, 也不按未知长度卖。
    if duration < 1:
        return _error(400, "invalid_request_error",
                      "duration must be a positive integer (自动时长无法计价，请选一个具体秒数).")

    if not model:
        return _error(400, "invalid_request_error", "model is required.")
    if not prompt:
        return _error(400, "invalid_request_error", "prompt is required.")

    entry = _catalog().get(model)
    provider = provider_of(entry)
    if not provider_available(provider):
        return _error(
            404,
            "model_not_found",
            f"Model '{model}' is not available yet."
            if provider == BAILIAN
            else f"Model '{model}' is not offered.",
        )
    amount = quote(model, resolution, duration)
    if amount is None:
        return _error(
            404,
            "model_not_found",
            f"Model '{model}' at {resolution} is not offered for video generation.",
        )

    # 预扣的前提: 现在就得有钱。余额不足直接拒, 别让上游先跑起来。
    if credits.balance(user["id"]) < amount:
        return _error(
            402,
            "insufficient_credits",
            f"这条视频需要 {amount} 积分，当前余额不足。",
        )

    try:
        task_id, err, code = await submit_video(
            provider,
            model,
            prompt,
            resolution,
            duration,
            str(body.get("image_url") or ""),
            ratio,
            media,
        )
    except httpx.HTTPError as exc:
        log.warning("视频作业提交失败: %s", exc)
        return _error(502, "upstream_error", "Upstream did not accept the job.")
    if err is not None:
        # 上游的参数校验原样转达 —— duration/resolution 的合法档位没有文档也没有
        # API, 厂商原话比我们猜的白名单准。此路径未扣费。
        return JSONResponse(err, status_code=code or 502)

    # 到这里作业已经在上游跑了, 钱花出去了 —— 现在扣。
    job_id = security.new_id("vjob_")
    credits.spend(user["id"], amount, kind="video", model=model, request_id=job_id)
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO video_jobs (id, user_id, model, upstream_task_id, status, prompt, "
            "duration, resolution, credits, refunded, url, error, provider, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,'','',?,?,?)",
            (
                job_id,
                user["id"],
                model,
                task_id,
                "processing",
                prompt[:2000],
                duration,
                resolution,
                amount,
                provider,
                now,
                now,
            ),
        )
    return JSONResponse(
        {"id": job_id, "model": model, "task_status": "PROCESSING", "video_result": None, "credits": amount}
    )


async def _settle(job: dict) -> dict:
    """向上游问一次并落定这个作业, 返回更新后的行。

    **客户端轮询与服务端兜底循环共用这一段**。分成两份实现必然漂, 而漂的后果是
    钱: 一边退款一边不退, 或者两边都退。

    上游一次抖动不落终态 —— 保持 processing, 下次再问。作业状态本来就是最终
    一致的, 把一次网络抖动当成失败会白退钱。
    """
    if job["status"] in ("succeeded", "failed"):
        return job

    # provider 从作业行读, 不从配置反查 —— 见 db.py 的迁移说明。老行没有这一列
    # (迁移前建的), 回落到千面: 那时只有千面。
    provider = str(job.get("provider") or QIANMIAN)
    state = await poll_video(provider, str(job["upstream_task_id"]))
    now = time.time()

    if state["status"] == "processing":
        # 还在跑 —— 但太久没有终态就当它废了并退款。上游偶尔会把作业丢掉 (既不
        # succeeded 也不 failed, 就是不动), 不设上限那笔钱永远悬着。
        # ⚠️ 这段必须在「processing 就返回」**之前**生效, 否则整条超时保护是死代码
        # (2026-08-28 重构时我把它写死过一次, 被 test_a_job_upstream_forgot 抓到)。
        if now - float(job["created"] or now) > config.VIDEO_JOB_MAX_AGE_S:
            message = f"上游超过 {int(config.VIDEO_JOB_MAX_AGE_S / 60)} 分钟未给出结果"
            log.warning("视频作业 %s %s, 判失败并退款", job["id"], message)
            with db.tx() as conn:
                conn.execute(
                    "UPDATE video_jobs SET status = 'failed', error = ?, updated = ? WHERE id = ?",
                    (message, now, job["id"]),
                )
            _refund_once(job)
            return {**job, "status": "failed", "error": message}
        return job

    if state["status"] == "succeeded":
        video_url = state["url"]
        with db.tx() as conn:
            conn.execute(
                "UPDATE video_jobs SET status = 'succeeded', url = ?, updated = ? WHERE id = ?",
                (video_url, now, job["id"]),
            )
        return {**job, "status": "succeeded", "url": video_url}

    if state["status"] == "failed":
        message = state["error"][:500]
        with db.tx() as conn:
            conn.execute(
                "UPDATE video_jobs SET status = 'failed', error = ?, updated = ? WHERE id = ?",
                (message, now, job["id"]),
            )
        _refund_once(job)
        return {**job, "status": "failed", "error": message}

    return job


def _job_response(job: dict) -> JSONResponse:
    if job["status"] == "succeeded":
        return JSONResponse(
            {
                "id": job["id"],
                "task_status": "SUCCESS",
                "video_result": [{"url": job["url"], "cover_image_url": ""}],
            }
        )
    if job["status"] == "failed":
        return JSONResponse(
            {
                "id": job["id"],
                "task_status": "FAIL",
                "video_result": None,
                "error": job["error"],
            }
        )
    return JSONResponse({"id": job["id"], "task_status": "PROCESSING", "video_result": None})


async def reconcile_tick() -> int:
    """把没人认领的作业收尾。返回落定的条数。

    **作业的生命周期不能只靠客户端轮询驱动。** 浏览器一关、ComfyUI 一报错、
    网络一抖, 作业就永远停在 processing —— 而钱是**提交时就扣掉**的:
    失败不退款, 成功也不记账。

    2026-08-27 实测: 两条 1080p 作业卡住, 各扣 600 积分。其中一条上游明明
    succeeded, 另一条上游 failed (内容审核), 13 小时无人退款 —— 只因为节点
    在轮询时撞上一次 502 就放弃了。
    """
    rows = db.query("SELECT * FROM video_jobs WHERE status = 'processing' ORDER BY created LIMIT 50")
    settled = 0
    for row in rows:
        job = dict(row)
        try:
            after = await _settle(job)
        except Exception:  # noqa: BLE001 — 一条坏账不能让整个循环停摆
            log.exception("收尾视频作业 %s 失败", job["id"])
            continue
        if after["status"] != job["status"]:
            settled += 1
            log.info("视频作业 %s 收尾为 %s", job["id"], after["status"])
    # 顺手扫掉过期的中转素材。搭在这个循环上而不是另起一个: 它已经是每分钟一次的
    # "媒体侧收尾"了, 再加一个循环只是多一处会忘的地方。
    try:
        gone = sweep_uploads()
        if gone:
            log.info("清掉 %d 个过期的中转素材", gone)
    except Exception:  # noqa: BLE001 — 清理失败不能让作业收尾停摆
        log.exception("清理中转素材失败")
    return settled


async def reconcile_loop() -> None:
    log.info(
        "视频作业收尾循环启动 (每 %ss 一次, 超过 %s 分钟未出结果判失败并退款)",
        config.VIDEO_RECONCILE_INTERVAL_S,
        int(config.VIDEO_JOB_MAX_AGE_S / 60),
    )
    while True:
        try:
            await asyncio.sleep(config.VIDEO_RECONCILE_INTERVAL_S)
            await reconcile_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("视频作业收尾循环出错")  # 绝不退出


@router.get("/v1/videos/result/{job_id}")
async def video_result(job_id: str, user: dict = Depends(resolve_user)):
    job = _job_row(job_id, user["id"])
    if job is None:
        return _error(404, "not_found", "No such video job.")
    return _job_response(await _settle(dict(job)))


@router.post("/v1/images/generations")
async def create_image(request: Request, user: dict = Depends(resolve_user)):
    """图生成是**同步**的 (实测 ~15 秒出图), 所以不进 video_jobs, 一个请求打完。

    上游的图像端点其实**返回 usage** (output_tokens 全是 image_tokens), 理论上
    能走 charge_credits 那条按 token 的路 —— 但那要求模型在 models.json 目录里,
    而它不在 (gen_models.py 的 SKIP_SUBSTRINGS 跳掉了 -image)。不在目录时
    charge_credits 会按"最贵条目"兜底, 不会漏计费, 但价格离谱。所以这里和视频
    一样按件计价, 口径统一, 也不用动聊天那条路。
    """
    gated = _gate(user)
    if gated is not None:
        return gated

    blocked = plans.check_run_blocked(user["id"])
    if blocked:
        return _error(402, "insufficient_credits", "余额不足或已达额度上限。")

    try:
        raw = await read_limited_body(
            request,
            max_bytes=config.GATEWAY_BODY_MAX_BYTES,
            timeout_s=config.REQUEST_BODY_TIMEOUT_S,
        )
        body = json.loads(raw)
    except json.JSONDecodeError:
        return _error(400, "invalid_request_error", "Body must be JSON.")

    model = str(body.get("model") or "")
    prompt = str(body.get("prompt") or "")
    try:
        n = max(1, min(int(body.get("n") or 1), config.IMAGE_MAX_BATCH))
    except (TypeError, ValueError):
        return _error(400, "invalid_request_error", "n must be an integer.")
    if not prompt:
        return _error(400, "invalid_request_error", "prompt is required.")

    entry = _image_catalog().get(model)
    # 两种计价都算在售: 千面有 usage 走 token, 百炼没有只能按张 (image_credits
    # 里那条分叉)。只认 token 单价的话, 百炼那半边模型永远进不来 —— gen_image
    # 里的百炼适配就是这么写完却没人调用的。
    if not _image_priced(entry or {}):
        return _error(404, "model_not_found", f"Model '{model}' is not offered for images.")

    provider = provider_of(entry)
    if not provider_available(provider):
        return _error(404, "model_not_found", f"Model '{model}' is not available yet.")
    if provider == QIANMIAN and not config.UPSTREAM_API_KEY:
        return _error(503, "upstream_unconfigured", "Image generation is not configured.")

    # 准入与聊天同口径: 出图前只看"有没有余额", 真实费用出图后按 usage 结算 ——
    # 出图前算不出价钱, 因为 token 数取决于画质与尺寸。
    if credits.balance(user["id"]) <= 0:
        return _error(402, "insufficient_credits", "余额不足。")

    extra = {
        key: body[key]
        for key in ("size", "quality", "background", "output_format", "image_url")
        if body.get(key) is not None
    }
    try:
        result, err, code = await gen_image(provider, model, prompt, n, extra)
    except httpx.HTTPError as exc:
        log.warning("图像生成失败: %s", exc)
        return _error(502, "upstream_error", "Upstream did not answer.")
    if err is not None:
        return JSONResponse(err, status_code=code or 502)

    # 出图了才扣 —— 上游报错的路径上一分不收。
    amount = image_credits(entry, result.get("usage"), str(extra.get("size") or ""))
    request_id = security.new_id("img_")
    credits.spend(
        user["id"],
        amount,
        kind="image",
        model=model,
        request_id=request_id,
        output=int((result.get("usage") or {}).get("output_tokens") or 0),
    )
    result["credits"] = amount
    return JSONResponse(result)


# ---- 素材上传: 官方节点的 image / video / audio 输入 ----
#
# ComfyUI 的官方节点在把素材交给厂商之前, 先上传到 comfy.org 换一个 URL:
#
#   POST /customers/storage  {file_name, content_type}  -> {upload_url, download_url}
#   PUT  <upload_url>        <原始字节>
#   然后把 **download_url** 放进给厂商的请求里 (Wan3 的 input.media[].url 等)
#
# --comfy-api-base 指向我们的垫片, 所以这条链路也落在我们身上。2026-08-28 之前
# 没实现, 于是**所有带素材输入的官方节点都是死的** —— 表现是节点 0 秒失败, 报
# 一句「该节点在执行过程中发生错误」, 而垫片日志里写的是「厂商 storage 未接入」,
# 那句话本身就是胡说 (storage 根本不是厂商)。
#
# 关键约束: download_url 必须**上游厂商能从公网抓到**。回环 (垫片自己那个 /blob)
# 和 VPC 内网都不行 —— 阿里云的服务器要去 GET 它。所以取回那一端是**无鉴权**的,
# 靠 id 不可猜来防遍历, 靠 TTL 限制暴露窗口。
_UPLOAD_DIR = config.DATA_DIR / "media_uploads"
# 只收媒体。不做白名单的话, 这就是一个挂在自家域名下、任何付费账号都能往里塞
# 任意文件的公开文件站 —— text/html 还能拿来做同源钓鱼。
_UPLOAD_TYPES = ("image/", "video/", "audio/")


def _upload_paths(blob_id: str) -> tuple[Path, Path]:
    return _UPLOAD_DIR / f"{blob_id}.bin", _UPLOAD_DIR / f"{blob_id}.json"


def _safe_blob_id(blob_id: str) -> str:
    """只认自己发出去的形状。任何路径成分都不接受 —— 这个 id 会被拼进文件名。"""
    ok = blob_id and len(blob_id) <= 64 and all(c.isalnum() or c in "-_" for c in blob_id)
    return blob_id if ok else ""


def sweep_uploads(now: float | None = None) -> int:
    """删掉过期的中转素材。只在这儿删 —— 上游抓完就不需要了, 留着纯属暴露面。"""
    now = time.time() if now is None else now
    removed = 0
    if not _UPLOAD_DIR.is_dir():
        return 0
    for f in _UPLOAD_DIR.iterdir():
        try:
            if now - f.stat().st_mtime > config.MEDIA_UPLOAD_TTL_S:
                f.unlink()
                removed += 1
        except OSError:
            continue
    return removed


@router.post("/v1/media/uploads")
async def create_upload(request: Request, user: dict = Depends(resolve_user)):
    """开一个上传位, 返回「往哪 PUT」和「厂商去哪取」。"""
    gated = _gate(user)
    if gated is not None:
        return gated
    try:
        raw = await read_limited_body(request, max_bytes=8192, timeout_s=config.REQUEST_BODY_TIMEOUT_S)
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _error(400, "invalid_request_error", "Body must be JSON.")
    ctype = str(body.get("content_type") or "application/octet-stream").split(";")[0].strip().lower()
    if not ctype.startswith(_UPLOAD_TYPES):
        return _error(
            415, "unsupported_media_type",
            f"只接受图片/视频/音频 (收到 {ctype})。",
        )
    blob_id = security.new_id()  # 96 位随机十六进制 —— 取回那端无鉴权, 全靠它猜不到
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _, meta = _upload_paths(blob_id)
    meta.write_text(json.dumps({
        "user_id": user["id"],
        "content_type": ctype,
        "file_name": str(body.get("file_name") or "")[:200],
        "created": time.time(),
    }))
    base = config.PUBLIC_BASE.rstrip("/")
    return JSONResponse({
        "id": blob_id,
        "upload_url": f"{base}/llm/v1/media/uploads/{blob_id}",
        "download_url": f"{base}/llm/v1/media/blobs/{blob_id}",
    })


@router.put("/v1/media/uploads/{blob_id}")
async def put_upload(blob_id: str, request: Request, user: dict = Depends(resolve_user)):
    blob_id = _safe_blob_id(blob_id)
    blob, meta = _upload_paths(blob_id) if blob_id else (None, None)
    if not blob_id or not meta.is_file():
        return _error(404, "not_found", "没有这个上传位。")
    info = json.loads(meta.read_text() or "{}")
    if info.get("user_id") != user["id"]:
        # 别人的上传位 —— 与「不存在」同一个回答, 不让人拿它探 id 是否存在。
        return _error(404, "not_found", "没有这个上传位。")
    data = await read_limited_body(
        request, max_bytes=config.MEDIA_UPLOAD_MAX_BYTES, timeout_s=config.REQUEST_BODY_TIMEOUT_S
    )
    blob.write_bytes(data)
    log.info("素材中转 %s 收到 %d 字节 (%s)", blob_id, len(data), info.get("content_type"))
    return JSONResponse({"id": blob_id, "bytes": len(data)})


@router.get("/v1/media/blobs/{blob_id}")
async def get_blob(blob_id: str):
    """**无鉴权** —— 上游厂商的服务器要来抓这个 URL, 它没有我们的令牌。

    安全性靠三条: id 不可猜、TTL 到点就删、Content-Type 只允许媒体且禁止嗅探。
    """
    blob_id = _safe_blob_id(blob_id)
    if not blob_id:
        return _error(404, "not_found", "没有这个素材。")
    blob, meta = _upload_paths(blob_id)
    if not blob.is_file() or not meta.is_file():
        return _error(404, "not_found", "没有这个素材。")
    info = json.loads(meta.read_text() or "{}")
    ctype = str(info.get("content_type") or "")
    if not ctype.startswith(_UPLOAD_TYPES):
        # 理论上进不来 (创建时挡过一次), 但这是**公开**出口, 再挡一次不亏。
        ctype = "application/octet-stream"
    return Response(
        blob.read_bytes(),
        media_type=ctype,
        # nosniff 由 security_headers 中间件统一加, 这里不重复设 ——
        # 重复的那一行没有任何测试能区分它在不在, 是死代码。
        # (test_blob_is_public_but_never_sniffable 验的是"这条路由确实被中间件覆盖到")
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "private, max-age=300",
        },
    )


@router.get("/v1/media/models")
async def media_models(user: dict = Depends(resolve_user)):
    """在售的媒体模型清单。ComfyUI 节点据此把"模型"做成下拉。

    走鉴权而不是公开: 灰度期这份清单本身就是未公开信息, 而节点在容器里本来就
    带着 token。
    """
    gated = _gate(user)
    if gated is not None:
        return gated
    return JSONResponse(offered())
