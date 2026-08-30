"""Focused contracts for route-aware request-body protection."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

_TMP = tempfile.mkdtemp(prefix="dhc-http-limits-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app.http_limits import RequestBodyLimit


def _sender(messages: list[dict]):
    async def send(message):
        messages.append(message)

    return send


def _scope(method: str, path: str, *, content_length: int | None = None) -> dict:
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("test", 443),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/auth/me"),
        ("HEAD", "/api/auth/me"),
        ("OPTIONS", "/api/auth/password"),
        ("POST", "/static/app.js"),
    ],
)
async def test_body_middleware_does_not_drain_safe_or_irrelevant_requests(method, path):
    receive_calls = 0

    async def downstream(scope, receive, send):
        del scope, receive
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("request body was read before the downstream app asked for it")

    sent = []
    middleware = RequestBodyLimit(downstream, default_bytes=128, webhook_bytes=64)
    await middleware(_scope(method, path, content_length=10_000), receive, _sender(sent))

    assert receive_calls == 0
    assert sent[0]["status"] == 204


@pytest.mark.asyncio
async def test_body_middleware_has_distinct_gateway_and_preview_limits():
    called = []

    async def downstream(scope, receive, send):
        called.append(scope["path"])
        await receive()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"x", "more_body": False}

    middleware = RequestBodyLimit(
        downstream,
        default_bytes=128,
        webhook_bytes=64,
        gateway_bytes=1024,
        preview_bytes=2048,
        receive_timeout_s=1,
    )

    for path, size, expected_status in (
        ("/api/auth/login", 512, 413),
        ("/llm/v1/chat/completions", 512, 204),
        # 向量化的报文是**整批**文本, 走 2MB 的通用上限会在知识库导入时 413,
        # 而 413 在 Coze 里只显示成"处理失败"。
        ("/llm/v1/embeddings", 512, 204),
        ("/llm/anthropic/v1/messages", 512, 204),
        ("/preview/8080/upload", 1500, 204),
        ("/api/pay/webhook/stripe", 65, 413),
    ):
        sent = []
        await middleware(_scope("POST", path, content_length=size), receive, _sender(sent))
        assert sent[0]["status"] == expected_status, path

    assert called == [
        "/llm/v1/chat/completions",
        "/llm/v1/embeddings",
        "/llm/anthropic/v1/messages",
        "/preview/8080/upload",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/llm/v1/chat/completions",
        "/llm/v1/embeddings",
        "/llm/anthropic/v1/messages",
        "/preview/8080/upload",
    ],
)
async def test_high_capacity_routes_are_not_buffered_before_downstream_auth(path):
    receive_calls = 0

    async def downstream(scope, receive, send):
        del scope, receive
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    chunks = iter(
        [
            {"type": "http.request", "body": b"unauthenticated", "more_body": True},
            {"type": "http.request", "body": b" payload", "more_body": False},
        ]
    )

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return next(chunks)

    sent = []
    middleware = RequestBodyLimit(
        downstream,
        default_bytes=128,
        webhook_bytes=64,
        gateway_bytes=1024,
        preview_bytes=2048,
        receive_timeout_s=1,
    )
    await middleware(_scope("POST", path), receive, _sender(sent))

    assert sent[0]["status"] == 401
    assert receive_calls == 0


@pytest.mark.asyncio
async def test_authenticated_body_reader_rejects_chunked_total_above_limit():
    from app import http_limits

    reader = getattr(http_limits, "read_limited_body", None)
    assert reader is not None, "high-capacity routes need a post-auth body limiter"

    chunks = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def receive():
        return next(chunks)

    request = Request(_scope("POST", "/llm/v1/chat/completions"), receive)
    with pytest.raises(HTTPException) as exc_info:
        await reader(request, max_bytes=5, timeout_s=1)

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "request_body_too_large"


@pytest.mark.asyncio
async def test_authenticated_body_reader_has_one_total_receive_deadline():
    from app.http_limits import read_limited_body

    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"1", "more_body": True}
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"2", "more_body": False}

    request = Request(_scope("POST", "/preview/8080/upload"), receive)
    with pytest.raises(HTTPException) as exc_info:
        await read_limited_body(request, max_bytes=10, timeout_s=0.01)

    assert exc_info.value.status_code == 408
    assert exc_info.value.detail == "request_body_timeout"


@pytest.mark.asyncio
async def test_chunked_body_must_complete_within_receive_deadline():
    calls = 0

    async def downstream(scope, receive, send):
        del scope, receive, send
        raise AssertionError("timed-out body reached the application")

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"x", "more_body": True}
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"y", "more_body": False}

    sent = []
    middleware = RequestBodyLimit(
        downstream,
        default_bytes=128,
        webhook_bytes=64,
        receive_timeout_s=0.01,
    )
    await middleware(_scope("POST", "/api/auth/login"), receive, _sender(sent))

    assert sent[0]["status"] == 408
    assert sent[-1]["body"] == b'{"detail":"request_body_timeout"}'
