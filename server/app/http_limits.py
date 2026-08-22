"""Route-aware request-body limits for small APIs and authenticated uploads."""

from __future__ import annotations

import asyncio

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import config


async def read_limited_body(request: Request, *, max_bytes: int, timeout_s: float) -> bytes:
    """Read a high-capacity route body after its authentication checks."""
    limit = max(0, int(max_bytes))
    chunks: list[bytes] = []
    seen = 0
    try:
        async with asyncio.timeout(max(0.001, float(timeout_s))):
            async for chunk in request.stream():
                seen += len(chunk)
                if seen > limit:
                    raise HTTPException(status_code=413, detail="request_body_too_large")
                chunks.append(chunk)
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail="request_body_timeout") from exc
    return b"".join(chunks)


class RequestBodyLimit:
    """Reject oversized HTTP bodies, including chunked requests.

    Content-Length is checked without reading. Small API and webhook bodies are
    then bounded and replayed here; gateway and preview bodies remain unread
    until their route has authenticated the caller and invokes
    ``read_limited_body``.
    """

    def __init__(
        self,
        app: ASGIApp,
        default_bytes: int,
        webhook_bytes: int,
        gateway_bytes: int | None = None,
        preview_bytes: int | None = None,
        receive_timeout_s: float | None = None,
    ):
        self.app = app
        self.default_bytes = max(0, int(default_bytes))
        self.webhook_bytes = max(0, int(webhook_bytes))
        self.gateway_bytes = max(
            0,
            int(config.GATEWAY_BODY_MAX_BYTES if gateway_bytes is None else gateway_bytes),
        )
        self.preview_bytes = max(
            0,
            int(config.PREVIEW_BODY_MAX_BYTES if preview_bytes is None else preview_bytes),
        )
        timeout = config.REQUEST_BODY_TIMEOUT_S if receive_timeout_s is None else receive_timeout_s
        self.receive_timeout_s = max(0.001, float(timeout))

    def _limit(self, method: str, path: str) -> int | None:
        if method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return None
        if path.startswith("/api/pay/webhook/"):
            return self.webhook_bytes
        if path.rstrip("/") in {
            "/llm/v1/chat/completions",
            "/llm/anthropic/v1/messages",
        }:
            return self.gateway_bytes
        parts = path.split("/", 3)
        if len(parts) >= 3 and parts[1] == "preview" and parts[2].isdigit():
            return self.preview_bytes
        if path.startswith("/api/"):
            return self.default_bytes
        return None

    @staticmethod
    def _read_after_auth(method: str, path: str) -> bool:
        if method.upper() in {"GET", "HEAD", "OPTIONS"}:
            return False
        if path.rstrip("/") in {
            "/llm/v1/chat/completions",
            "/llm/anthropic/v1/messages",
        }:
            return True
        parts = path.split("/", 3)
        return len(parts) >= 3 and parts[1] == "preview" and parts[2].isdigit()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self._limit(scope.get("method", ""), scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"")
        if raw_length.isdigit() and int(raw_length) > limit:
            await self._reject(send)
            return

        if self._read_after_auth(scope.get("method", ""), scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        buffered: list[Message] = []
        seen = 0
        try:
            async with asyncio.timeout(self.receive_timeout_s):
                while True:
                    message = await receive()
                    buffered.append(message)
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        continue
                    seen += len(message.get("body", b""))
                    if seen > limit:
                        await self._reject(send)
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await self._reject_timeout(send)
            return

        index = 0

        async def replay_receive() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            # Do not invent a disconnect after the final body chunk. Streaming
            # responses and BaseHTTPMiddleware may keep listening for the real
            # client disconnect while they send; delegating preserves that ASGI
            # lifetime signal instead of cancelling an otherwise healthy stream.
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"detail":"request_body_too_large"}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _reject_timeout(send: Send) -> None:
        body = b'{"detail":"request_body_timeout"}'
        await send(
            {
                "type": "http.response.start",
                "status": 408,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
