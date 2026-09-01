"""Route-aware response hardening that preserves stricter endpoint policies."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: 数字人通话页。它是全站唯一要开麦、并且用 MediaSource (blob:) 放视频的页面,
#: 下面两处安全头为它开了口子。改路由记得同步这里 —— 不同步的表现是通话静默失灵。
AVATAR_PATH = "/avatar"


class SecurityHeaders:
    def __init__(self, app: ASGIApp, https: bool, work_host: str = ""):
        self.app = app
        self.https = https
        # 工作台域整域豁免 CSP: 那上面跑的是 dsh 的应用 (上游代码), 它的打包产物
        # 用 new Function() —— script-src 不带 'unsafe-eval' 就是让它启动即死,
        # 症状是整页白屏、只剩我们注入的外壳按钮。2026-08-23 生产实测踩过:
        # 无头浏览器里就是一条 EvalError, 而 curl -I 看不见 (HEAD 无 content-type,
        # CSP 分支根本不触发 —— 验证要用 GET)。
        # dsh 在自己的子域上, 与主站已经是不同源; 给它的 CSP 保护不了我们的会话,
        # 却会随上游任何一次构建方式变化而炸。
        self.work_host = (work_host or "").lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: C901
        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
                req_path = scope.get("path", "")
                # 数字人页是**靠说话用的** —— 它必须能开麦。其余页面维持全关。
                # 按路径开口子而不是全站放开: 这个头挡的正是"页面上某段代码偷偷
                # 开麦", 而只有这一页有正当理由。
                mic = "microphone=(self)" if req_path == AVATAR_PATH else "microphone=()"
                headers.setdefault("permissions-policy", f"camera=(), {mic}, geolocation=()")
                headers.setdefault("x-frame-options", "DENY")
                path = scope.get("path", "")
                content_type = headers.get("content-type", "")
                req_host = ""
                for k, v in scope.get("headers", []):
                    if k == b"host":
                        req_host = v.decode("latin-1").split(":")[0].lower()
                        break
                on_work = bool(self.work_host) and req_host == self.work_host
                if "text/html" in content_type and not path.startswith("/preview/") and not on_work:
                    headers.setdefault(
                        "content-security-policy",
                        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
                        "object-src 'none'; form-action 'self'; img-src 'self' data: https:; "
                        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
                        # 数字人页的视频源是 MediaSource 的 blob: URL。没有这一条,
                        # default-src 'self' 会把它挡掉 —— 而表现是"画面一帧不动":
                        # WebSocket 照常收字节、计时照走、积分照扣, 只有浏览器控制台
                        # 里有一行 CSP 违规。查了很久才落到这里。
                        + ("; media-src 'self' blob:" if path == AVATAR_PATH else ""),
                    )
                if self.https:
                    headers.setdefault(
                        "strict-transport-security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, secure_send)
