"""模型中心 —— 自托管推理节点的引擎、显卡与模型装卸。

从口袋专家 (AgentsDanceCloud) 迁过来的第一块。那边的控制台有三页 (模型广场 /
模型样本 / 模型训练 / 模型部署) 直接在浏览器里打推理节点的 `/v1/*`；那条路走不通
到这边来, 原因有两条:

  · 那三页是 React/Next, 而这里是 Jinja + 一点原生 JS, 栈不一样;
  · 更要紧的是**浏览器直连推理节点**要求用户手上有那个节点的凭证。这里沿用
    数字人那条的做法: 凭证只在服务端 (见 gateway.py 的同一条纪律), 页面只打
    `/api/models-hub/*`, 由我们代转。

与工作台产品**结构上不同**: 没有每用户容器, 也就没有机时计费 —— 它是只读看板
加两个装卸动作, 打的是我们自己那台推理机。

⛔ **只代转白名单里的那几条路径**。推理节点上还有 `/v1/chat/completions`、
`/v1/embeddings` 这些真花钱的推理面, 一旦让页面能透传任意路径, 这一页就成了绕开
积分计费的免费推理入口。白名单在 `_ALLOWED`, 加路径前先想清楚它会不会被这么用。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config
from .accounts import resolve_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["models-hub"])

#: 代转超时。装载一个模型可能要拉权重, 比普通请求慢得多, 所以读超时给得宽;
#: 连接超时仍然短 —— 节点没起来就该立刻说, 别让人对着转圈等三十秒。
_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)

#: ⛔ 能代转的路径白名单 (方法, 路径前缀)。只放**看板与装卸**, 不放任何推理面。
#: 放开 /v1/chat/completions 这类 = 把这一页变成绕开积分计费的免费推理入口。
_ALLOWED: tuple[tuple[str, str], ...] = (
    ("GET", "/v1/engines"),
    ("GET", "/v1/gpu"),
    ("GET", "/v1/models"),
    ("GET", "/v1/models/base"),
    ("POST", "/v1/models/register"),
    ("POST", "/v1/models/unregister"),
)

#: 带引擎名的那两条单独判 (`/v1/models/{engine}/load` / `unload`) —— 引擎名是用户
#: 给的, 不能用前缀匹配放行, 否则 `/v1/models/../../anything` 也能过。
_ENGINE_ACTIONS = ("load", "unload")


def configured() -> bool:
    """推理节点配好了没。没配 = 这一格不上线 (卡片仍在, 点进来告诉人还没接)。"""
    return bool(_base_url())


def _base_url() -> str:
    return str(getattr(config, "INFERENCE_URL", "") or "").rstrip("/")


def _headers() -> dict:
    """节点凭证**只在服务端**。与 gateway.py 同一条: 上游 key 一个字节都不下发。"""
    key = str(getattr(config, "INFERENCE_KEY", "") or "")
    return {"X-API-Key": key} if key else {}


def allowed_target(method: str, path: str) -> str:
    """把页面给的 (方法, 路径) 校成一条真能打的上游路径; 不合法抛 404。

    ⛔ 这里是这一页唯一的安全边界。判据是**白名单**而不是"黑名单排除推理面" ——
    黑名单会随着上游加接口而漏, 白名单不会。
    """
    method = str(method or "").upper()
    path = "/" + str(path or "").strip().lstrip("/")
    # 冗余加固: 白名单本身已经挡住了这些 (`/v1/models/../x` 段数对不上, `//v1/...`
    # 归一化后也不在表里)。留着是因为它零成本, **但别把它当成防线** —— 松了白名单,
    # 这两行一条也拦不住。注红时这条不变红是对的。
    if ".." in path or "//" in path:
        raise HTTPException(status_code=404, detail="不支持的路径")
    if (method, path) in _ALLOWED:
        return path
    # /v1/models/<engine>/load|unload
    parts = path.split("/")
    if (
        method == "POST"
        and len(parts) == 5
        and parts[1] == "v1"
        and parts[2] == "models"
        and parts[3]
        and parts[4] in _ENGINE_ACTIONS
    ):
        return path
    raise HTTPException(status_code=404, detail="不支持的路径")


@router.get("/api/models-hub/status")
def models_hub_status(user: dict = Depends(resolve_user)) -> JSONResponse:
    """这一格接没接上。页面先问这个, 没接就摆一句人话而不是一片转圈。"""
    return JSONResponse({"configured": configured()})


@router.api_route("/api/models-hub/proxy", methods=["GET", "POST"])
async def models_hub_proxy(request: Request, user: dict = Depends(resolve_user)):
    """代转到推理节点。页面把要打的路径放在 `?path=`, body 原样带过去。

    为什么不一条路径一个端点: 上游那套 `/v1/*` 会随节点升级增减, 一条一个端点的话
    每次都要改两边。用一个代转口 + 一份白名单, 加接口只改 `_ALLOWED` 一行。
    """
    if not configured():
        raise HTTPException(status_code=503, detail="推理节点尚未配置")
    target = allowed_target(request.method, request.query_params.get("path", ""))
    body = await request.body() if request.method == "POST" else None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            upstream = await http.request(
                request.method,
                _base_url() + target,
                headers={**_headers(), "Content-Type": "application/json"},
                content=body,
            )
    except httpx.HTTPError as exc:
        # ⛔ 不把异常原文回给页面: 里面可能带内网地址。日志里留全的。
        logger.warning("[models-hub] 代转失败 %s %s: %s", request.method, target, exc)
        raise HTTPException(status_code=502, detail="推理节点没有应答") from exc
    media = upstream.headers.get("content-type", "application/json")
    return (
        JSONResponse(
            content=_safe_json(upstream),
            status_code=upstream.status_code,
        )
        if media.startswith("application/json")
        else JSONResponse(content={"detail": "推理节点返回了非 JSON 内容"}, status_code=502)
    )


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return {"detail": "推理节点返回的不是合法 JSON"}
