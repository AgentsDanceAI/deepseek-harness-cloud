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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .accounts import resolve_user
from .upstream import Upstream

router = APIRouter(tags=["models-hub"])

#: ⛔ 能代转的路径白名单。只放**看板与装卸**, 不放任何推理面 ——
#: 放开 /v1/chat/completions 这类 = 把这一页变成绕开积分计费的免费推理入口。
_UP = Upstream(
    "推理节点",
    "INFERENCE_URL",
    "INFERENCE_KEY",
    allowed=(
        ("GET", "/v1/engines"),
        ("GET", "/v1/gpu"),
        ("GET", "/v1/models"),
        ("GET", "/v1/models/base"),
        ("POST", "/v1/models/register"),
        ("POST", "/v1/models/unregister"),
    ),
    #: 带引擎名的那两条: 引擎名是用户给的, 段数钉死才不会被 /v1/models/../../x 绕过。
    prefix_allowed=(("POST", "v1/models", 5),),
)

#: 引擎动作只认这两个 —— 前缀白名单管不到最后一段叫什么。
_ENGINE_ACTIONS = ("load", "unload")


def configured() -> bool:
    """推理节点配好了没。没配 = 这一格不上线 (卡片仍在, 点进来告诉人还没接)。"""
    return _UP.configured()


def allowed_target(method: str, path: str) -> str:
    """校成一条真能打的上游路径; 不合法抛 404。

    ⛔ 这里是这一页唯一的安全边界。判据是**白名单**而不是"黑名单排除推理面" ——
    黑名单会随着上游加接口而漏, 白名单不会。
    """
    got = _UP.target(method, path)
    parts = got.split("?", 1)[0].split("/")
    if len(parts) == 5 and parts[2] == "models" and parts[4] not in _ENGINE_ACTIONS:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="不支持的路径")
    return got


@router.get("/api/models-hub/status")
def models_hub_status(user: dict = Depends(resolve_user)) -> JSONResponse:
    """这一格接没接上。页面先问这个, 没接就摆一句人话而不是一片转圈。"""
    return JSONResponse({"configured": configured()})


@router.api_route("/api/models-hub/proxy", methods=["GET", "POST"])
async def models_hub_proxy(request: Request, user: dict = Depends(resolve_user)):
    """代转到推理节点。页面把要打的路径放在 `?path=`, body 原样带过去。"""
    allowed_target(request.method, request.query_params.get("path", ""))
    return await _UP.forward(request, request.query_params.get("path", ""))
