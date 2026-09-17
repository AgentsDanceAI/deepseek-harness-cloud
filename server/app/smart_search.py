"""AI 智慧搜索 —— 对着一份索引提问, 召回 + 大模型总结。

自口袋专家 (AgentsDanceCloud) 迁过来的第二格。那边的控制台页是 React/Next, 这里按
本仓的栈重写成 Jinja + 一点原生 JS; 召回与总结仍在我们自己那台后端上, 页面通过
`/api/smart-search/proxy` 代转过去 (凭证只在服务端, 见 upstream.py)。

⛔ **为什么不把召回层也搬过来**: 那 697 行召回 (`advanced_search.py`) 被口袋专家的
专家工具 (`dataset_search` / `dataset_aggregate` / `dataset_get`) **直接 import**。
搬走 = 把专家的检索能力一起搬走。真要两边各有一份, 第一步是把它剥成中立模块, 不是
复制一遍 —— 复制出来的两份必然漂, 而漂的表现是"同一个索引在两个产品里搜出不同结果"。
所以这一格现在是**界面在这边、数据面在那边**。

## 还差什么 (上架前要解决)

⚠️ **单租户**。上游按租户隔离索引并按额度计费, 而我们这边拿一把服务端密钥 ——
所有访客共用同一个上游租户。因此现在 unlisted (拿链接直达)。要开给所有人, 得把访客
身份带到上游 (上游认 X-API-Key/session, 需要一条"AI Store 用户 → 上游租户"的映射)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .accounts import resolve_user
from .upstream import Upstream

router = APIRouter(tags=["smart-search"])

#: ⛔ 只放**读**的那几条。上游同一台后端上还有建索引、写字段、删数据的口子, 以及
#: /v1/chat/completions 这类花钱的推理面 —— 白名单一松, 这一格就是后门。
_UP = Upstream(
    "AI 智慧搜索",
    "PE_API_URL",
    "PE_API_KEY",
    allowed=(
        ("GET", "/api/datasets"),  # 有哪些索引
        ("GET", "/api/ai-search"),  # 召回 + 总结 (SSE)
        ("GET", "/api/suggest"),  # 输入联想
        ("POST", "/api/search-advanced"),
    ),
    prefix_allowed=(
        ("GET", "api/datasets", 5),  # /api/datasets/<index>/detail —— 段数钉死
        ("GET", "api/fields", 4),  # /api/fields/<index>
    ),
)


def configured() -> bool:
    return _UP.configured()


@router.get("/api/smart-search/status")
def smart_search_status(user: dict = Depends(resolve_user)) -> JSONResponse:
    return JSONResponse({"configured": configured()})


@router.api_route("/api/smart-search/proxy", methods=["GET", "POST"])
async def smart_search_proxy(request: Request, user: dict = Depends(resolve_user)):
    """代转到我们自己的检索后端。页面把要打的路径放在 `?path=`。

    ⚠️ `/api/ai-search` 是 **SSE**: 总结是一个字一个字出来的, 代转必须边收边回
    (见 upstream.forward) —— 整段读完再回等于把流式产品做成了转圈产品。
    """
    path = request.query_params.get("path", "")
    # 页面把上游自己的查询串放在 path 里 (它本来就带 ?query=&index=), 原样带过去
    return await _UP.forward(request, path)
