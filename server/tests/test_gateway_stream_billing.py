"""Regression tests for conservative billing of interrupted LLM streams."""

from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile

import httpx
import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

_TMP = tempfile.mkdtemp(prefix="dhc-gateway-stream-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import gateway


class _StreamResponse:
    def __init__(self, status: int, chunks: list[bytes], detail: bytes = b""):
        self.status_code = status
        self._chunks = chunks
        self._detail = detail

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return self._detail


class _BlockedFirstChunkResponse(_StreamResponse):
    def __init__(self):
        super().__init__(200, [])
        self.waiting = asyncio.Event()

    async def aiter_raw(self):
        self.waiting.set()
        await asyncio.Future()
        yield b""  # pragma: no cover - cancellation is the behavior under test


class _StreamContext:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.response

    async def __aexit__(self, *_args):
        return False


class _Client:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.last_kwargs = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, _method, _url, **kwargs):
        self.last_kwargs = kwargs
        return _StreamContext(self.response, self.error)


def _request(path: str, body: dict) -> Request:
    raw = json.dumps(body).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        },
        receive,
    )


@pytest.fixture()
def gateway_user(monkeypatch):
    monkeypatch.setattr(gateway.config, "UPSTREAM_API_KEY", "test-upstream-key")
    monkeypatch.setattr(gateway, "_admit", lambda _user: None)
    return {"id": "stream-billing-user", "device_id": "device"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path"),
    [
        (gateway.chat_completions, "/llm/v1/chat/completions"),
        (gateway.anthropic_messages, "/llm/anthropic/v1/messages"),
    ],
)
async def test_gateway_routes_enforce_body_limit_after_admission(gateway_user, monkeypatch, handler, path):
    monkeypatch.setattr(gateway.config, "GATEWAY_BODY_MAX_BYTES", 5)
    monkeypatch.setattr(gateway.config, "REQUEST_BODY_TIMEOUT_S", 1)
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "zhipu")
    monkeypatch.setattr(gateway.config, "ZHIPU_SEARCH_API_KEY", "")

    with pytest.raises(HTTPException) as exc_info:
        await handler(_request(path, {"model": "definitely-not-offered"}), gateway_user)

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "request_body_too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500])
async def test_openai_upstream_error_stream_is_never_billed(gateway_user, monkeypatch, status):
    spends = []
    response = _StreamResponse(status, [], b'{"error":{"message":"rejected"}}')
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _Client(response))
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await gateway.chat_completions(
        _request(
            "/llm/v1/chat/completions",
            {"model": "deepseek-v4-flash", "stream": True, "messages": [{"content": "hello"}]},
        ),
        gateway_user,
    )
    payload = b"".join([chunk async for chunk in result.body_iterator])

    assert b"upstream_error" in payload
    assert spends == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path"),
    [
        (gateway.chat_completions, "/llm/v1/chat/completions"),
        (gateway.anthropic_messages, "/llm/anthropic/v1/messages"),
    ],
)
async def test_connection_failure_before_stream_is_never_billed(gateway_user, monkeypatch, handler, path):
    spends = []
    error = httpx.ConnectError("connection failed", request=httpx.Request("POST", "https://upstream"))
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _Client(error=error))
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await handler(
        _request(
            path,
            {"model": "deepseek-v4-flash", "stream": True, "messages": [{"content": "hello"}]},
        ),
        gateway_user,
    )
    with pytest.raises(httpx.ConnectError):
        _ = [chunk async for chunk in result.body_iterator]

    assert spends == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path"),
    [
        (gateway.chat_completions, "/llm/v1/chat/completions"),
        (gateway.anthropic_messages, "/llm/anthropic/v1/messages"),
    ],
)
async def test_cancel_after_2xx_before_first_chunk_bills_request_fallback(
    gateway_user, monkeypatch, handler, path
):
    spends = []
    upstream = _BlockedFirstChunkResponse()
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _Client(upstream))
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await handler(
        _request(
            path,
            {
                "model": "deepseek-v4-flash",
                "stream": True,
                "messages": [{"content": "full request must still be billed"}],
            },
        ),
        gateway_user,
    )
    first_chunk = asyncio.create_task(anext(result.body_iterator))
    await upstream.waiting.wait()
    first_chunk.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_chunk

    assert len(spends) == 1
    assert spends[0][1]["uncached_input"] > 0
    assert spends[0][1]["output"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "path"),
    [
        (gateway.chat_completions, "/llm/v1/chat/completions"),
        (gateway.anthropic_messages, "/llm/anthropic/v1/messages"),
    ],
)
async def test_naturally_empty_success_stream_is_not_billed(gateway_user, monkeypatch, handler, path):
    spends = []
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _Client(_StreamResponse(200, [])))
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await handler(
        _request(
            path,
            {"model": "deepseek-v4-flash", "stream": True, "messages": [{"content": "hello"}]},
        ),
        gateway_user,
    )
    assert [chunk async for chunk in result.body_iterator] == []
    assert spends == []


@pytest.mark.asyncio
async def test_openai_fallback_counts_top_level_instructions_and_tools(gateway_user, monkeypatch):
    spends = []
    fake_client = _Client(_StreamResponse(200, [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n']))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: fake_client)
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await gateway.chat_completions(
        _request(
            "/llm/v1/chat/completions",
            {
                "model": "deepseek-v4-flash",
                "stream": True,
                "instructions": "s" * 4000,
                "tools": [{"type": "function", "function": {"description": "t" * 8000}}],
                "messages": [{"role": "user", "content": "hello"}],
            },
        ),
        gateway_user,
    )
    _ = [chunk async for chunk in result.body_iterator]

    sent_body = fake_client.last_kwargs["json"]
    expected_prompt = math.ceil(
        len(json.dumps(sent_body, ensure_ascii=False).encode()) / gateway.STREAM_FALLBACK_BYTES_PER_TOKEN
    )
    assert spends[0][1]["uncached_input"] == expected_prompt
    assert spends[0][1]["output"] > 0


@pytest.mark.asyncio
async def test_anthropic_disconnect_combines_known_input_with_fallback_output(gateway_user, monkeypatch):
    spends = []
    chunks = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":123}}}\n\n',
        b'data: {"type":"content_block_delta","delta":{"text":"' + b"x" * 8000 + b'"}}\n\n',
        b'data: {"type":"message_delta","usage":{"output_tokens":2000}}\n\n',
    ]
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _Client(_StreamResponse(200, chunks)))
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await gateway.anthropic_messages(
        _request(
            "/llm/anthropic/v1/messages",
            {"model": "deepseek-v4-flash", "stream": True, "messages": [{"content": "hello"}]},
        ),
        gateway_user,
    )
    iterator = result.body_iterator
    await anext(iterator)
    await anext(iterator)
    await iterator.aclose()

    assert spends[0][1]["uncached_input"] == 123
    assert spends[0][1]["output"] > 0


@pytest.mark.asyncio
async def test_anthropic_completed_stream_keeps_provider_usage(gateway_user, monkeypatch):
    spends = []
    chunks = [
        b'data: {"type":"message_start","message":{"usage":{"input_tokens":123}}}\n\n',
        b'data: {"type":"content_block_delta","delta":{"text":"' + b"x" * 8000 + b'"}}\n\n',
        b'data: {"type":"message_delta","usage":{"output_tokens":50}}\n\n',
        b'data: {"type":"message_stop"}\n\n',
    ]
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _Client(_StreamResponse(200, chunks)))
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await gateway.anthropic_messages(
        _request(
            "/llm/anthropic/v1/messages",
            {"model": "deepseek-v4-flash", "stream": True, "messages": [{"content": "hello"}]},
        ),
        gateway_user,
    )
    _ = [chunk async for chunk in result.body_iterator]

    assert spends[0][1]["uncached_input"] == 123
    assert spends[0][1]["output"] == 50


@pytest.mark.asyncio
async def test_anthropic_upstream_error_stream_is_never_billed(gateway_user, monkeypatch):
    spends = []
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(
        gateway,
        "_upstream_client",
        lambda: _Client(_StreamResponse(400, [], b'{"error":{"message":"bad request"}}')),
    )
    monkeypatch.setattr(gateway.credits, "spend", lambda *args, **kwargs: spends.append((args, kwargs)))

    result = await gateway.anthropic_messages(
        _request(
            "/llm/anthropic/v1/messages",
            {"model": "deepseek-v4-flash", "stream": True, "messages": [{"content": "hello"}]},
        ),
        gateway_user,
    )
    payload = b"".join([chunk async for chunk in result.body_iterator])

    assert b"upstream_error" in payload
    assert spends == []
