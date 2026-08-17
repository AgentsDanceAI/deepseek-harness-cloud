"""Concurrency limit for installer downloads.

The installers live on this machine and are large (the mac build is 282MB).
Nothing else here competes for bandwidth on that scale, so a handful of
simultaneous downloads is enough to starve the model gateway and the cloud
workspaces running on the same box — the paying surface of the product would
degrade because someone opened four download tabs.

This does not make the machine a good CDN. It bounds the damage: at most
MAX_CONCURRENT transfers at a time, at most PER_IP from any single address, and
anyone over the limit gets a 503 with Retry-After rather than a slow trickle
that holds a worker open for minutes.

Written as raw ASGI rather than BaseHTTPMiddleware on purpose. BaseHTTPMiddleware
hands control back as soon as the response STARTS, so releasing the slot in a
`finally` there frees it before a single byte of a 282MB file has been sent —
the limit then only ever sees the handshake and never rejects anything (measured:
three overlapping transfers all admitted under a per-IP cap of two). Here the
slot is held until the final body message, which is the window that actually
costs bandwidth.

Deliberately not a byte-rate limiter: throttling each stream would keep every
connection alive LONGER, which is the opposite of what protects the box.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

MAX_CONCURRENT = 4   # whole machine
PER_IP = 2           # one person resuming a download should not use the budget
STALE_AFTER = 1800   # a slot older than this is assumed leaked, not in flight

_BUSY_BODY = json.dumps(
    {"detail": "download_busy", "message": "下载通道繁忙，请 30 秒后重试。"},
).encode()


class ReleaseThrottle:
    def __init__(self, app, prefix: str = "/releases/"):
        self.app = app
        self.prefix = prefix
        self._active: dict[str, list[float]] = defaultdict(list)

    def _client(self, scope) -> str:
        # Behind Caddy, so the socket peer is always the proxy. Fall back to it
        # only when the header is absent (direct access in dev).
        for name, value in scope.get("headers") or ():
            if name == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _prune(self, now: float) -> int:
        """Drop leaked slots; return the live total.

        A dropped connection can leave its slot behind, and without this the
        limit would ratchet down to zero over time and refuse everyone.
        """
        total = 0
        for ip in list(self._active):
            fresh = [t for t in self._active[ip] if now - t < STALE_AFTER]
            if fresh:
                self._active[ip] = fresh
                total += len(fresh)
            else:
                del self._active[ip]
        return total

    def _release(self, ip: str) -> None:
        slots = self._active.get(ip)
        if slots:
            slots.pop()
            if not slots:
                self._active.pop(ip, None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        now = time.time()
        ip = self._client(scope)
        total = self._prune(now)
        if total >= MAX_CONCURRENT or len(self._active[ip]) >= PER_IP:
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"retry-after", b"30")]})
            await send({"type": "http.response.body", "body": _BUSY_BODY})
            return

        self._active[ip].append(now)
        released = False

        async def send_wrapper(message):
            nonlocal released
            await send(message)
            if (message["type"] == "http.response.body"
                    and not message.get("more_body", False)
                    and not released):
                released = True
                self._release(ip)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Covers the client-disconnect path, where the final body message
            # never arrives.
            if not released:
                self._release(ip)
