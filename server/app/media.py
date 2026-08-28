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
from fastapi.responses import JSONResponse

from . import config, credits, db, model_catalog, plans, security
from .accounts import resolve_user
from .http_limits import read_limited_body

log = logging.getLogger(__name__)
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
        if any((m.get("credits_per_second") or {}).values())
    ]
    image = [
        {"id": m["id"], "name": m.get("name") or m["id"]}
        for m in _image_catalog().values()
        if m.get("usd_per_1m_image_tokens")
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


def image_credits(entry: dict, usage: dict | None) -> int:
    """按 usage 里的真实 token 数计价。

    图像**不按张收**: 同一个模型低画质与高画质相差 35 倍
    (gpt-image-2 一张 1024²: $0.006 vs $0.211), 按张收要么坑用户要么亏钱, 而
    token 数会如实反映画质与尺寸。口径与聊天一致 —— 同一个 CREDITS_PER_USD
    与 MODEL_PRICE_MARKUP, 所以两条线的毛利率天然一致。

    上游没给 usage 时回落到 fallback_credits: 宁可贵也不能免费。
    """
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
    try:
        duration = int(body.get("duration") or config.VIDEO_DEFAULT_DURATION)
    except (TypeError, ValueError):
        return _error(400, "invalid_request_error", "duration must be an integer.")

    if not model:
        return _error(400, "invalid_request_error", "model is required.")
    if not prompt:
        return _error(400, "invalid_request_error", "prompt is required.")

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

    payload = {"model": model, "prompt": prompt, "duration": duration, "resolution": resolution}
    for passthrough in ("image_url", "ratio", "seed", "camera_fixed", "watermark"):
        if body.get(passthrough) is not None:
            payload[passthrough] = body[passthrough]

    url = config.UPSTREAM_BASE_URL.rstrip("/") + "/video/generations"
    try:
        async with _client() as client:
            upstream = await client.post(url, headers=_auth_headers(), json=payload)
    except httpx.HTTPError as exc:
        log.warning("视频作业提交失败: %s", exc)
        return _error(502, "upstream_error", "Upstream did not accept the job.")

    if upstream.status_code >= 400:
        # 上游的参数校验原样转达 —— duration/resolution 的合法档位没有文档也没有
        # API, 厂商原话比我们猜的白名单准。此路径未扣费。
        try:
            detail = upstream.json()
        except ValueError:
            detail = {"error": {"message": upstream.text[:400]}}
        return JSONResponse(detail, status_code=upstream.status_code)

    try:
        task_id = str(upstream.json()["task_id"])
    except (ValueError, KeyError):
        return _error(502, "upstream_error", "Upstream returned no task id.")

    # 到这里作业已经在上游跑了, 钱花出去了 —— 现在扣。
    job_id = security.new_id("vjob_")
    credits.spend(user["id"], amount, kind="video", model=model, request_id=job_id)
    now = time.time()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO video_jobs (id, user_id, model, upstream_task_id, status, prompt, "
            "duration, resolution, credits, refunded, url, error, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,0,'','',?,?)",
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

    url = config.UPSTREAM_BASE_URL.rstrip("/") + f"/video/generations/{job['upstream_task_id']}"
    try:
        async with _client() as client:
            upstream = await client.get(url, headers=_auth_headers())
        data = (upstream.json() or {}).get("data") or {}
    except (httpx.HTTPError, ValueError):
        return job

    upstream_status = str(data.get("status") or "").lower()
    now = time.time()

    if upstream_status == "succeeded":
        video_url = str(data.get("url") or "")
        with db.tx() as conn:
            conn.execute(
                "UPDATE video_jobs SET status = 'succeeded', url = ?, updated = ? WHERE id = ?",
                (video_url, now, job["id"]),
            )
        return {**job, "status": "succeeded", "url": video_url}

    if upstream_status in ("failed", "error", "cancelled"):
        raw = data.get("error")
        message = str(raw if not isinstance(raw, dict) else json.dumps(raw, ensure_ascii=False))[:500]
        with db.tx() as conn:
            conn.execute(
                "UPDATE video_jobs SET status = 'failed', error = ?, updated = ? WHERE id = ?",
                (message, now, job["id"]),
            )
        _refund_once(job)
        return {**job, "status": "failed", "error": message}

    # 太久没有终态就当它废了并退款。上游偶尔会把作业丢掉 (既不 succeeded 也不
    # failed, 就是不动), 不设上限的话那笔钱永远悬着。
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
    if not config.UPSTREAM_API_KEY:
        return _error(503, "upstream_unconfigured", "Image generation is not configured.")
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
    if not (entry or {}).get("usd_per_1m_image_tokens"):
        return _error(404, "model_not_found", f"Model '{model}' is not offered for images.")

    # 准入与聊天同口径: 出图前只看"有没有余额", 真实费用出图后按 usage 结算 ——
    # 出图前算不出价钱, 因为 token 数取决于画质与尺寸。
    if credits.balance(user["id"]) <= 0:
        return _error(402, "insufficient_credits", "余额不足。")

    payload = {"model": model, "prompt": prompt, "n": n}
    for passthrough in ("size", "quality", "background", "output_format", "image_url"):
        if body.get(passthrough) is not None:
            payload[passthrough] = body[passthrough]

    url = config.UPSTREAM_BASE_URL.rstrip("/") + "/images/generations"
    try:
        async with _client() as client:
            upstream = await client.post(url, headers=_auth_headers(), json=payload)
    except httpx.HTTPError as exc:
        log.warning("图像生成失败: %s", exc)
        return _error(502, "upstream_error", "Upstream did not answer.")

    if upstream.status_code >= 400:
        try:
            detail = upstream.json()
        except ValueError:
            detail = {"error": {"message": upstream.text[:400]}}
        return JSONResponse(detail, status_code=upstream.status_code)

    try:
        result = upstream.json()
    except ValueError:
        return _error(502, "upstream_error", "Upstream returned invalid JSON.")

    # 出图了才扣 —— 上游报错的路径上一分不收。
    amount = image_credits(entry, result.get("usage"))
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
