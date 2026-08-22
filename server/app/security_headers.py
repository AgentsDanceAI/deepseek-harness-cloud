"""Route-aware response hardening that preserves stricter endpoint policies."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeaders:
    def __init__(self, app: ASGIApp, https: bool):
        self.app = app
        self.https = https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("referrer-policy", "strict-origin-when-cross-origin")
                headers.setdefault("permissions-policy", "camera=(), microphone=(), geolocation=()")
                headers.setdefault("x-frame-options", "DENY")
                path = scope.get("path", "")
                content_type = headers.get("content-type", "")
                if "text/html" in content_type and not path.startswith("/preview/"):
                    headers.setdefault(
                        "content-security-policy",
                        "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
                        "object-src 'none'; form-action 'self'; img-src 'self' data: https:; "
                        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
                    )
                if self.https:
                    headers.setdefault(
                        "strict-transport-security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, secure_send)
