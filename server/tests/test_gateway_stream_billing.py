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


def test_body_shape_records_structure_not_content():
    """诊断摘要只记结构, 不记正文。

    上游拒一个请求时只会说一句它自己的话 ("The content field is a required
    field."), 而我们这边看不见自己发出去的是什么 —— 同样的报错在任何客户端上
    都长一个样, 只能靠猜。2026-08-31 Coze 工作流那个 400 就是这么排不动的。
    但消息正文是用户数据, 一个字都不能进日志。
    """
    body = {
        "model": "m1",
        "messages": [
            {"role": "system", "content": "绝密的系统提示词"},
            {"role": "user", "content": [{"type": "text", "text": "用户的私密问题"}]},
            {"role": "assistant", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "content": None, "tool_call_id": "x"},
        ],
        "tools": [{"type": "function"}],
        "stream": True,
    }
    out = gateway._body_shape(body)
    # 结构说清楚了
    assert "system:content=str" in out
    assert "user:content=list[1]" in out
    assert "assistant:content=missing+role,tool_calls" in out
    assert "tool:content=null" in out
    assert "tools=list" in out and "stream=bool" in out
    # 正文一个字都没有
    for secret in ("绝密的系统提示词", "用户的私密问题"):
        assert secret not in out, "把用户内容写进日志了"


def test_body_shape_survives_junk():
    """畸形请求也不能让诊断本身抛异常 —— 那会把一次 400 变成 500。"""
    for junk in (None, "字符串", [1, 2], {"messages": "不是列表"}, {"messages": [1, None]}):
        gateway._body_shape(junk)


def test_responses_billing_reads_the_right_usage_fields():
    """Responses 面的用量字段名跟 chat 面**不一样**, 认错就是白送。

    chat: prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens
    responses: input_tokens / output_tokens / input_tokens_details.cached_tokens
    照抄 chat 那套字段名的话, 每次都会走"取不到用量"的兜底 (按字节估), 而估出来
    的数跟真实用量差很远 —— 用户少付, 我们照付。
    """
    import inspect

    from app import gateway

    src = inspect.getsource(gateway.responses)
    assert "input_tokens" in src and "output_tokens" in src
    assert "input_tokens_details" in src, "缓存命中字段用的是 responses 那套吗"
    assert "prompt_tokens" not in src, "抄了 chat 面的字段名 -> 永远取不到用量"
    # 流式的用量挂在收尾事件的 response 里, 少数实现挂顶层, 两处都要认
    assert '(parsed.get("response") or {}).get("usage")' in src


def test_responses_uses_the_responses_upstream_path():
    """必须打上游的 /responses, 不是 /chat/completions。

    Codex 从某个版本起不再支持 wire_api="chat", 只认 Responses API; 打错路径的
    症状是它一直转圈 (实测卡了十分钟没有任何输出)。
    """
    import inspect

    from app import gateway

    src = inspect.getsource(gateway.responses)
    assert 'rstrip("/") + "/responses"' in src


# ---- Anthropic 面的在售校验 (2026-08-31) -----------------------------------


@pytest.mark.asyncio
async def test_anthropic_rejects_models_outside_the_catalog(gateway_user, monkeypatch):
    """不在售的型号一律拒。

    这面早先是放行的 —— 它诞生时只服务 web_search (走另一分支, 不转发), 转发是
    后加的。放行的代价: 目录外的名字在 charge_credits 里走兜底价, 用户为一个我们
    从没上架的型号付一个我们没标过的价 (同一个洞在 Gemini 面上实测到过一笔)。
    Claude Code 允许用户 /model 随便打, 所以这是常态而非例外。
    """
    spends = []
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(gateway.model_catalog, "resolve", lambda _mid: None)
    monkeypatch.setattr(gateway.credits, "spend", lambda *a, **kw: spends.append(kw))

    result = await gateway.anthropic_messages(
        _request(
            "/llm/anthropic/v1/messages",
            {"model": "claude-not-on-our-menu", "messages": [{"content": "hi"}]},
        ),
        gateway_user,
    )

    assert result.status_code == 404
    assert spends == []


@pytest.mark.asyncio
async def test_anthropic_rewrites_the_model_to_the_upstream_name(gateway_user, monkeypatch):
    """牌名与上游型号名不同的话, 转发的 body 里必须换成后者。"""
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "upstream")
    monkeypatch.setattr(
        gateway.model_catalog,
        "resolve",
        lambda mid: {"id": mid, "upstream_model": "vendor-internal"},
    )
    monkeypatch.setattr(gateway.model_catalog, "charge_credits", lambda *a: 0)
    monkeypatch.setattr(gateway.credits, "spend", lambda *a, **kw: None)
    client = _Client(_StreamResponse(200, [b"event: message_stop\n"]))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)

    result = await gateway.anthropic_messages(
        _request(
            "/llm/anthropic/v1/messages",
            {"model": "claude-sonnet-5", "stream": True, "messages": [{"content": "hi"}]},
        ),
        gateway_user,
    )
    [chunk async for chunk in result.body_iterator]

    assert json.loads(client.last_kwargs["content"])["model"] == "vendor-internal"
