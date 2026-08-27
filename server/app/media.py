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

import json
import logging
import math
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import config, credits, db, plans, security
from .accounts import resolve_user
from .http_limits import read_limited_body

log = logging.getLogger(__name__)
router = APIRouter(prefix="/llm")

_PRICES_PATH = Path(__file__).resolve().parents[1] / "config" / "video_models.json"
_prices_cache: dict | None = None


def _catalog() -> dict:
    """{model_id: {name, credits_per_second: {resolution: credits|None}}}"""
    global _prices_cache
    if _prices_cache is None:
        try:
            raw = json.loads(_PRICES_PATH.read_text(encoding="utf-8"))
            _prices_cache = {m["id"]: m for m in raw.get("models", [])}
        except (OSError, ValueError, KeyError):
            log.exception("video_models.json 读不了, 视频功能关闭")
            _prices_cache = {}
    return _prices_cache


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": code, "message": message}}, status_code=status)


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


@router.get("/v1/videos/result/{job_id}")
async def video_result(job_id: str, user: dict = Depends(resolve_user)):
    job = _job_row(job_id, user["id"])
    if job is None:
        return _error(404, "not_found", "No such video job.")
    job = dict(job)

    status = str(job["status"])
    if status == "succeeded":
        return JSONResponse(
            {
                "id": job_id,
                "task_status": "SUCCESS",
                "video_result": [{"url": job["url"], "cover_image_url": ""}],
            }
        )
    if status == "failed":
        return JSONResponse(
            {
                "id": job_id,
                "task_status": "FAIL",
                "video_result": None,
                "error": job["error"],
            }
        )

    url = config.UPSTREAM_BASE_URL.rstrip("/") + f"/video/generations/{job['upstream_task_id']}"
    try:
        async with _client() as client:
            upstream = await client.get(url, headers=_auth_headers())
        data = (upstream.json() or {}).get("data") or {}
    except (httpx.HTTPError, ValueError):
        # 上游一次抖动不该让作业变成终态 —— 保持 processing, 下次轮询再看。
        return JSONResponse({"id": job_id, "task_status": "PROCESSING", "video_result": None})

    upstream_status = str(data.get("status") or "").lower()
    now = time.time()

    if upstream_status == "succeeded":
        video_url = str(data.get("url") or "")
        with db.tx() as conn:
            conn.execute(
                "UPDATE video_jobs SET status = 'succeeded', url = ?, updated = ? WHERE id = ?",
                (video_url, now, job_id),
            )
        return JSONResponse(
            {
                "id": job_id,
                "task_status": "SUCCESS",
                "video_result": [{"url": video_url, "cover_image_url": ""}],
            }
        )

    if upstream_status in ("failed", "error", "cancelled"):
        message = str(
            (data.get("error") or {}) if isinstance(data.get("error"), dict) else data.get("error") or ""
        )[:500]
        with db.tx() as conn:
            conn.execute(
                "UPDATE video_jobs SET status = 'failed', error = ?, updated = ? WHERE id = ?",
                (message, now, job_id),
            )
        _refund_once(job)
        return JSONResponse(
            {
                "id": job_id,
                "task_status": "FAIL",
                "video_result": None,
                "error": message,
            }
        )

    return JSONResponse({"id": job_id, "task_status": "PROCESSING", "video_result": None})
