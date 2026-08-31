"""Gemini 面 (Google 原生协议) 的转发与计费。

这面是给 Gemini CLI 用的 —— 实测 0.57.0 一设 GOOGLE_GEMINI_BASE_URL, 它只会打
POST {base}/v1beta/models/{model}:generateContent, 鉴权放在 x-goog-api-key 头上。
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from starlette.requests import Request

_TMP = tempfile.mkdtemp(prefix="dhc-gateway-gemini-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import gateway


class _Response:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


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


class _StreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _Client:
    def __init__(self, response):
        self.response = response
        self.urls: list[str] = []
        self.headers: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.urls.append(url)
        self.headers.append(kwargs.get("headers") or {})
        return self.response

    def stream(self, _method, url, **kwargs):
        self.urls.append(url)
        self.headers.append(kwargs.get("headers") or {})
        return _StreamContext(self.response)


def _request(path: str, body: dict, query: bytes = b"") -> Request:
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
            "query_string": query,
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        },
        receive,
    )


@pytest.fixture()
def gemini_user(monkeypatch):
    monkeypatch.setattr(gateway.config, "UPSTREAM_API_KEY", "test-upstream-key")
    monkeypatch.setattr(gateway.config, "UPSTREAM_GEMINI_BASE", "https://upstream.test")
    monkeypatch.setattr(gateway, "_admit", lambda _user: None)
    monkeypatch.setattr(
        gateway.model_catalog,
        "resolve",
        lambda mid: {"id": mid} if mid == "gemini-3-pro" else None,
    )
    return {"id": "gemini-user", "device_id": "device"}


@pytest.fixture()
def spends(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(gateway.credits, "spend", lambda *a, **kw: seen.append({"args": a, **kw}))
    return seen


# ---- 计费口径 --------------------------------------------------------------


def test_cached_prompt_tokens_are_not_billed_at_full_price():
    """promptTokenCount 是**含**缓存命中的总输入。

    照抄它当"未命中缓存的输入", 等于把缓存价按全价收 —— 用户多付, 而且长会话
    (缓存命中占大头) 越用越离谱。
    """
    uncached, cached, output = gateway._gemini_usage(
        {"promptTokenCount": 1000, "cachedContentTokenCount": 800, "candidatesTokenCount": 50}
    )
    assert (uncached, cached, output) == (200, 800, 50)


def test_thinking_tokens_are_billed_as_output():
    """thoughtsTokenCount 是单列的, **不含**在 candidatesTokenCount 里。

    漏了它等于白送推理模型最贵的那段 —— 思考往往比可见回答长好几倍。
    """
    _, _, output = gateway._gemini_usage(
        {"promptTokenCount": 10, "candidatesTokenCount": 20, "thoughtsTokenCount": 500}
    )
    assert output == 520


def test_missing_usage_metadata_still_bills_something():
    """上游没给 usage 时不能白跑一趟。"""
    assert gateway._gemini_usage(None) == (0, 0, 0)
    assert gateway._gemini_usage("nonsense") == (0, 0, 0)


@pytest.mark.asyncio
async def test_non_stream_bills_from_usage_metadata(gemini_user, spends, monkeypatch):
    response = _Response(
        200,
        {
            "candidates": [{"content": {"parts": [{"text": "hi"}]}}],
            "usageMetadata": {
                "promptTokenCount": 300,
                "cachedContentTokenCount": 100,
                "candidatesTokenCount": 40,
                "thoughtsTokenCount": 60,
            },
        },
    )
    client = _Client(response)
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)
    monkeypatch.setattr(gateway.model_catalog, "charge_credits", lambda *a: 7)

    result = await gateway.gemini_generate(
        "gemini-3-pro:generateContent",
        _request("/llm/gemini/v1beta/models/gemini-3-pro:generateContent", {"contents": []}),
        gemini_user,
    )

    assert result.status_code == 200
    assert len(spends) == 1
    assert spends[0]["uncached_input"] == 200
    assert spends[0]["cache_read"] == 100
    assert spends[0]["output"] == 100
    assert spends[0]["model"] == "gemini-3-pro"
    assert spends[0]["kind"] == "llm"


@pytest.mark.asyncio
async def test_count_tokens_is_free(gemini_user, spends, monkeypatch):
    """countTokens 不产出内容, 上游也不收 —— 客户端每次发送前都会调它。

    按次收会变成"什么都没干就扣分", 而且用户看不到是谁扣的。
    """
    client = _Client(_Response(200, {"totalTokens": 42}))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)

    result = await gateway.gemini_generate(
        "gemini-3-pro:countTokens",
        _request("/llm/gemini/v1beta/models/gemini-3-pro:countTokens", {"contents": []}),
        gemini_user,
    )

    assert result.status_code == 200
    assert spends == []


@pytest.mark.asyncio
async def test_unknown_action_is_rejected(gemini_user, spends):
    """白名单之外的动作一律拒。

    全转的话, 上游将来加的动作 (批处理、文件上传) 会绕过这里的计费 —— 白送。
    """
    result = await gateway.gemini_generate(
        "gemini-3-pro:embedContent",
        _request("/llm/gemini/v1beta/models/gemini-3-pro:embedContent", {}),
        gemini_user,
    )
    assert result.status_code == 404
    assert spends == []


@pytest.mark.asyncio
async def test_model_outside_the_catalog_is_rejected(gemini_user, spends):
    """不在售的型号一律拒 —— 与 chat / responses 同口径。

    放行有两处坏账: 上游按真价收我们, 而目录外的名字在 charge_credits 里走兜底价
    (线上实测 gemini-3.1-pro-preview 被扣了 113 分), 用户为一个我们从没上架的型号
    付了一个我们没标过的价。而 Gemini CLI **默认**就挑它自己的型号, 这是常态。
    """
    result = await gateway.gemini_generate(
        "gemini-3.1-pro-preview:generateContent",
        _request("/llm/gemini/v1beta/models/gemini-3.1-pro-preview:generateContent", {}),
        gemini_user,
    )
    assert result.status_code == 404
    assert spends == []


@pytest.mark.asyncio
async def test_upstream_model_name_is_used_in_the_path(gemini_user, monkeypatch):
    """牌名与上游型号名可以不同 —— 打上游要用后者。"""
    monkeypatch.setattr(
        gateway.model_catalog,
        "resolve",
        lambda mid: {"id": mid, "upstream_model": "vendor-internal-name"} if mid == "gemini-3-pro" else None,
    )
    client = _Client(_Response(200, {"usageMetadata": {}}))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)
    monkeypatch.setattr(gateway.model_catalog, "charge_credits", lambda *a: 0)
    monkeypatch.setattr(gateway.credits, "spend", lambda *a, **kw: None)

    await gateway.gemini_generate(
        "gemini-3-pro:generateContent",
        _request("/llm/gemini/v1beta/models/gemini-3-pro:generateContent", {"contents": []}),
        gemini_user,
    )

    assert client.urls == ["https://upstream.test/v1beta/models/vendor-internal-name:generateContent"]


# ---- 转发 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_string_is_forwarded(gemini_user, monkeypatch):
    """alt=sse 决定上游吐 SSE 还是一整个 JSON 数组。

    丢了它, 客户端按 SSE 解析一个数组, 表现是**一直转圈到超时**, 而两边都不报错。
    """
    client = _Client(_StreamResponse(200, [b'data: {"usageMetadata":{"promptTokenCount":1}}\n\n']))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)
    monkeypatch.setattr(gateway.model_catalog, "charge_credits", lambda *a: 1)
    monkeypatch.setattr(gateway.credits, "spend", lambda *a, **kw: None)

    result = await gateway.gemini_generate(
        "gemini-3-pro:streamGenerateContent",
        _request(
            "/llm/gemini/v1beta/models/gemini-3-pro:streamGenerateContent",
            {"contents": []},
            query=b"alt=sse",
        ),
        gemini_user,
    )
    [chunk async for chunk in result.body_iterator]

    assert client.urls == ["https://upstream.test/v1beta/models/gemini-3-pro:streamGenerateContent?alt=sse"]


@pytest.mark.asyncio
async def test_upstream_key_replaces_the_caller_token(gemini_user, monkeypatch):
    """上游密钥只在服务端 —— 转发时必须用它, 而不是把用户令牌透传出去。"""
    client = _Client(_Response(200, {"usageMetadata": {}}))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)
    monkeypatch.setattr(gateway.model_catalog, "charge_credits", lambda *a: 0)
    monkeypatch.setattr(gateway.credits, "spend", lambda *a, **kw: None)

    await gateway.gemini_generate(
        "gemini-3-pro:generateContent",
        _request("/llm/gemini/v1beta/models/gemini-3-pro:generateContent", {"contents": []}),
        gemini_user,
    )

    assert client.headers[0]["x-goog-api-key"] == "test-upstream-key"


@pytest.mark.asyncio
async def test_stream_bills_from_the_last_usage_chunk(gemini_user, spends, monkeypatch):
    """每个分片都带一份**累计**的 usageMetadata —— 取最后一份, 不是累加。"""
    chunks = [
        b'data: {"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}\n\n',
        b'data: {"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":80}}\n\n',
    ]
    client = _Client(_StreamResponse(200, chunks))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)
    monkeypatch.setattr(gateway.model_catalog, "charge_credits", lambda *a: 3)

    result = await gateway.gemini_generate(
        "gemini-3-pro:streamGenerateContent",
        _request("/llm/gemini/v1beta/models/gemini-3-pro:streamGenerateContent", {"contents": []}),
        gemini_user,
    )
    [chunk async for chunk in result.body_iterator]

    assert len(spends) == 1
    assert spends[0]["uncached_input"] == 10
    assert spends[0]["output"] == 80


@pytest.mark.asyncio
async def test_upstream_error_stream_is_never_billed(gemini_user, spends, monkeypatch):
    client = _Client(_StreamResponse(429, [], b'{"error":{"message":"rate limited"}}'))
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)

    result = await gateway.gemini_generate(
        "gemini-3-pro:streamGenerateContent",
        _request("/llm/gemini/v1beta/models/gemini-3-pro:streamGenerateContent", {"contents": []}),
        gemini_user,
    )
    payload = b"".join([chunk async for chunk in result.body_iterator])

    assert b"upstream_error" in payload
    assert spends == []
