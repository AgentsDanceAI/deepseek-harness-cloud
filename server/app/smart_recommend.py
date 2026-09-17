"""AI 智慧推荐 —— 给一个用户画像出信息流, 或给一个物品出相关推荐。

自口袋专家 (AgentsDanceCloud) 迁过来的第三格。与 AI 智慧搜索同构: 界面按本仓的栈
重写, 召回与推荐理由仍在我们自己那台后端上, 页面通过 `/api/smart-recommend/proxy`
代转过去 (凭证只在服务端, 见 upstream.py)。

⚠️ 我一度以为这一格"逻辑自足、零依赖"—— **错的**。它和智搜一样要 Elasticsearch 与
租户级索引: 上游 `/api/recommend/feed` 第一件事就是 `_assert_index_owned(index)`。
两格共用同一套索引层, 所以要么一起留在那边的数据面上, 要么将来一起搬。

## 还差什么 (上架前要解决)

⚠️ **单租户**, 与智搜同一条: 上游按租户隔离并按额度计费, 我们这边是一把服务端密钥,
所有访客共用同一个上游租户。因此现在 unlisted (拿链接直达)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .accounts import resolve_user
from .upstream import Upstream

router = APIRouter(tags=["smart-recommend"])

#: ⛔ 只放这一格用得到的四条 + 索引清单。`feed` / `related` 是 SSE (推荐理由是流式的)。
_UP = Upstream(
    "AI 智慧推荐",
    "PE_API_URL",
    "PE_API_KEY",
    allowed=(
        ("GET", "/api/datasets"),
        ("GET", "/api/recommend/random-user"),
        ("GET", "/api/recommend/random-item"),
        ("POST", "/api/recommend/feed"),
        ("POST", "/api/recommend/related"),
    ),
    prefix_allowed=(
        ("GET", "api/datasets", 5),  # /api/datasets/<index>/detail
    ),
)


def configured() -> bool:
    return _UP.configured()


@router.get("/api/smart-recommend/status")
def smart_recommend_status(user: dict = Depends(resolve_user)) -> JSONResponse:
    return JSONResponse({"configured": configured()})


@router.api_route("/api/smart-recommend/proxy", methods=["GET", "POST"])
async def smart_recommend_proxy(request: Request, user: dict = Depends(resolve_user)):
    return await _UP.forward(request, request.query_params.get("path", ""))
