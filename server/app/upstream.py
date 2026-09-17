"""往**我们自己的后端**代转的公共件 —— 自口袋专家迁过来的那几格共用。

迁过来的三格 (模型中心 / AI 智慧搜索 / AI 智慧推荐) 形态相同: 页面住在主站上, 真正
干活的接口在我们自己那台后端上。三格各写一遍代转必然漂 —— 漂的那一处就是漏出去的
那一处, 所以抽在这里。

两条不可动摇的:

⛔ **上游凭证只在服务端**。页面打 `/api/<格>/proxy`, 一个字节的 key 都不下发
   (与 `gateway.py` 的上游 key 同一条纪律)。

⛔ **只代转白名单里的路径**。上游那台后端上还有别的面 —— 推理 (`/v1/chat/completions`)、
   写索引、删数据。能透传任意路径, 这几格就成了绕开计费与权限的后门。判据是**白名单**
   而不是"黑名单排除危险的": 黑名单会随着上游加接口而漏, 白名单不会。

⚠️ **目前是单租户的**。上游那几条接口按租户隔离并按额度计费, 而我们这边拿的是一把
   服务端密钥 —— 也就是说所有访客共用**同一个**上游租户的索引与账单。三格现在都
   未上架 (unlisted, 拿链接直达), 这个前提成立; 要真正开给所有人, 得先把访客身份
   带到上游去 (见各格模块注释里的"还差什么")。
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

#: 连接超时短、读超时长: 上游没起来要立刻说 (别让人对着转圈), 但一次召回 + 大模型
#: 总结可能真的要几十秒。
TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)


class Upstream:
    """一格的代转器: 认一个上游地址、一把服务端密钥、一份路径白名单。"""

    def __init__(self, name: str, url_attr: str, key_attr: str, allowed: tuple, prefix_allowed: tuple = ()):
        self.name = name
        self._url_attr = url_attr
        self._key_attr = key_attr
        #: (方法, 完整路径) 的精确白名单
        self.allowed = allowed
        #: (方法, 前缀, 段数) —— 路径里带用户给的变量时用。前缀是**从头算起的完整前缀**
        #: (如 "api/datasets"), 段数是 split("/") 之后的长度 (含开头那个空串):
        #: `/api/datasets/<index>/detail` 是 5。
        #: ⛔ **必须钉死段数** —— 只匹前缀等于放开整棵子树 (/api/datasets/x/y/delete 也会过)。
        self.prefix_allowed = prefix_allowed

    # ── 配置 ────────────────────────────────────────────────────────────
    def base_url(self) -> str:
        from . import config

        return str(getattr(config, self._url_attr, "") or "").rstrip("/")

    def configured(self) -> bool:
        """上游配好了没。没配 = 这一格不上线, 页面自己摆一句人话。"""
        return bool(self.base_url())

    def headers(self) -> dict:
        from . import config

        key = str(getattr(config, self._key_attr, "") or "")
        return {"X-API-Key": key} if key else {}

    # ── 白名单 ──────────────────────────────────────────────────────────
    def target(self, method: str, path: str) -> str:
        """校成一条真能打的上游路径; 不合法抛 404 (不回显白名单内容)。"""
        method = str(method or "").upper()
        path = "/" + str(path or "").strip().lstrip("/")
        if ".." in path or "//" in path:
            # 冗余加固: 下面的判据本来就挡得住 (段数/表里没有)。留着零成本,
            # **但别把它当防线** —— 松了白名单, 这一行一点忙都帮不上。
            raise HTTPException(status_code=404, detail="不支持的路径")
        base = path.split("?", 1)[0]
        if (method, base) in self.allowed:
            return path
        parts = base.split("/")  # "/api/datasets/x/detail" → ['', 'api', 'datasets', 'x', 'detail']
        for m, prefix, segs in self.prefix_allowed:
            if method != m or len(parts) != segs:
                continue
            want = [w for w in prefix.strip("/").split("/") if w]
            if parts[1 : 1 + len(want)] == want and all(parts[1:]):
                return path
        raise HTTPException(status_code=404, detail="不支持的路径")

    # ── 代转 ────────────────────────────────────────────────────────────
    async def forward(self, request: Request, path: str):
        """把这一次请求原样送到上游, 回应原样带回来。

        流式的 (SSE) 要**边收边回** —— 智搜与智推的正文都是一个字一个字出来的,
        整段读完再回等于把流式产品做成了转圈产品。
        """
        if not self.configured():
            raise HTTPException(status_code=503, detail=f"{self.name}尚未接入")
        target = self.target(request.method, path)
        body = await request.body() if request.method in ("POST", "PUT") else None
        url = self.base_url() + target
        headers = {**self.headers(), "Content-Type": "application/json"}
        client = httpx.AsyncClient(timeout=TIMEOUT)
        try:
            req = client.build_request(request.method, url, headers=headers, content=body)
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            # ⛔ 不回显异常原文: 里面可能带内网地址。全的留在日志里。
            logger.warning("[%s] 代转失败 %s %s: %s", self.name, request.method, target, exc)
            raise HTTPException(status_code=502, detail=f"{self.name}没有应答") from exc
        media = resp.headers.get("content-type", "application/json")
        if media.startswith("text/event-stream"):

            async def _pipe():
                try:
                    async for chunk in resp.aiter_raw():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                _pipe(),
                media_type="text/event-stream",
                status_code=resp.status_code,
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        try:
            await resp.aread()
            try:
                content = resp.json()
            except ValueError:
                content = {"detail": f"{self.name}返回的不是合法 JSON"}
            return JSONResponse(content=content, status_code=resp.status_code)
        finally:
            await resp.aclose()
            await client.aclose()
