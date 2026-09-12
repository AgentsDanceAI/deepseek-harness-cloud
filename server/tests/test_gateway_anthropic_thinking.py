"""Claude Code 的 body 经 anthropic 面到底转发成什么样。

2026-09-11 实测 (server/scripts/probe_anthropic_face.py, 每组 6 发): 上游中继把
同一个牌名**按请求轮询**到好几家后端, Bedrock 那路不收 `thinking.type=enabled`
(只认 adaptive), 而 Claude Code 每轮都带 —— `claude-sonnet-5` 原样只有 3/6 通过。
`claude-sonnet-5-thinking` 6/6, 且 6 发全落在直连 Anthropic。

于是两层:
  主路  带 thinking 的 claude 请求, 转发时换成 `<型号>-thinking` —— **不改 body**,
        语义一个字段都不动。
  兜底  上游仍回 400 的话, 削平 body 重试一次 (语义降级, 但好过让用户吃 400)。

这里钉住的是"转发出去的到底是什么", 不是"函数返回了什么" —— 所以每条都真跑一遍
handler, 从假上游收到的 kwargs 里把 body 读回来。
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
from starlette.requests import Request

_TMP = tempfile.mkdtemp(prefix="dhc-gw-think-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))

from app import gateway  # noqa: E402

#: Claude Code 2.1.x 真发的三样 (2026-09-11 本地 sink 抓的原样)。
CC_EXTRAS = {
    "thinking": {"type": "enabled", "budget_tokens": 1024},
    "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
    "output_config": {"effort": "medium"},
}


class _Resp:
    def __init__(self, status: int, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload

    async def aread(self) -> bytes:
        return self.text.encode()

    async def aiter_raw(self):
        yield b'data: {"type":"message_stop"}\n\n'


class _StreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *_a):
        return False


class _Client:
    """按调用次序返回预置响应, 并记下每一次发出去的 body。"""

    def __init__(self, responses: list[_Resp]):
        self._responses = list(responses)
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def _take(self, kwargs) -> _Resp:
        self.sent.append(json.loads(kwargs["content"]))
        return self._responses.pop(0) if self._responses else _Resp(200, {"usage": {}})

    async def post(self, _url, **kwargs):
        return self._take(kwargs)

    def stream(self, _method, _url, **kwargs):
        return _StreamCtx(self._take(kwargs))


def _request(body: dict) -> Request:
    raw = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/llm/anthropic/v1/messages",
            "raw_path": b"/llm/anthropic/v1/messages",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        },
        receive,
    )


@pytest.fixture()
def gw(monkeypatch):
    monkeypatch.setattr(gateway.config, "UPSTREAM_API_KEY", "k")
    monkeypatch.setattr(gateway.config, "UPSTREAM_ANTHROPIC_BASE", "https://upstream.test/v1")
    monkeypatch.setattr(gateway.config, "SEARCH_PROVIDER", "none")
    monkeypatch.setattr(gateway.config, "ANTHROPIC_PIN_THINKING_CHANNEL", True)
    monkeypatch.setattr(gateway, "_admit", lambda _u: None)
    monkeypatch.setattr(gateway.model_catalog, "resolve", lambda m: {"id": m, "upstream_model": m})
    monkeypatch.setattr(gateway.model_catalog, "charge_credits", lambda *a, **k: 0)
    spends: list[dict] = []
    monkeypatch.setattr(gateway.credits, "spend", lambda uid, amt, **kw: spends.append(kw))
    return {"user": {"id": "u", "device_id": "d"}, "spends": spends}


def _install(monkeypatch, client: _Client):
    monkeypatch.setattr(gateway, "_upstream_client", lambda: client)


def _cc_body(model: str = "claude-sonnet-5", **over) -> dict:
    body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    body.update(CC_EXTRAS)
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_thinking_request_is_pinned_to_the_thinking_channel(gw, monkeypatch):
    """主路: 换型号名, **body 其余部分原样** —— 语义不许降级。"""
    client = _Client([_Resp(200, {"usage": {"input_tokens": 1, "output_tokens": 1}})])
    _install(monkeypatch, client)

    await gateway.anthropic_messages(_request(_cc_body()), gw["user"])

    sent = client.sent[0]
    assert sent["model"] == "claude-sonnet-5-thinking", "没换到支持 thinking 的那路 -> 约一半请求 400"
    assert sent["thinking"] == CC_EXTRAS["thinking"], "换通道就够了, 不该动 thinking"
    assert sent["context_management"] == CC_EXTRAS["context_management"]
    assert sent["output_config"] == CC_EXTRAS["output_config"]


@pytest.mark.asyncio
async def test_billing_still_uses_the_shelf_name(gw, monkeypatch):
    """**账按牌名记。** 换的是转发名; 拿 `-thinking` 去计价等于按一个没标过价的
    名字收钱 (charge_credits 会走兜底价) —— 2026-08-31 Gemini 面上吃过这笔坏账。"""
    client = _Client([_Resp(200, {"usage": {"input_tokens": 3, "output_tokens": 2}})])
    _install(monkeypatch, client)

    await gateway.anthropic_messages(_request(_cc_body()), gw["user"])

    assert gw["spends"], "这一轮没记账"
    assert gw["spends"][-1]["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "why"),
    [
        (_cc_body(thinking={"type": "adaptive"}), "adaptive 家家都收, 不用挪到贵的那路"),
        ({"model": "claude-sonnet-5", "max_tokens": 8, "messages": []}, "没 thinking 的普通请求"),
        (_cc_body(model="gpt-5.6-luna"), "非 claude 系没有这个后缀约定"),
        (_cc_body(model="claude-sonnet-5-thinking"), "已经是了, 别加两遍"),
    ],
)
async def test_only_budgeted_thinking_claude_requests_are_moved(gw, monkeypatch, body, why):
    client = _Client([_Resp(200, {"usage": {}})])
    _install(monkeypatch, client)

    await gateway.anthropic_messages(_request(body), gw["user"])

    assert client.sent[0]["model"] == body["model"], why


@pytest.mark.asyncio
async def test_flag_off_forwards_verbatim(gw, monkeypatch):
    monkeypatch.setattr(gateway.config, "ANTHROPIC_PIN_THINKING_CHANNEL", False)
    client = _Client([_Resp(200, {"usage": {}})])
    _install(monkeypatch, client)

    await gateway.anthropic_messages(_request(_cc_body()), gw["user"])

    assert client.sent[0]["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_400_retries_once_with_a_flattened_body(gw, monkeypatch):
    """兜底: 上游仍回 400 就削平重试一次。这里**故意**关掉主路, 单独验兜底。"""
    monkeypatch.setattr(gateway.config, "ANTHROPIC_PIN_THINKING_CHANNEL", False)
    client = _Client(
        [
            _Resp(400, {"error": {"message": "thinking.type.enabled is not supported"}}),
            _Resp(200, {"usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )
    _install(monkeypatch, client)

    resp = await gateway.anthropic_messages(_request(_cc_body()), gw["user"])

    assert resp.status_code == 200, "400 之后没重试 —— 用户直接吃到错误"
    assert len(client.sent) == 2
    first, second = client.sent
    assert first["thinking"]["type"] == "enabled", "第一发必须是用户原样的"
    assert second["thinking"] == {"type": "adaptive"}
    assert "context_management" not in second
    assert "output_config" not in second


@pytest.mark.asyncio
async def test_400_is_not_retried_when_there_is_nothing_to_flatten(gw, monkeypatch):
    """削不动就别重试 —— 同一个坏请求打两遍只是把上游的 400 收两次。"""
    monkeypatch.setattr(gateway.config, "ANTHROPIC_PIN_THINKING_CHANNEL", False)
    client = _Client([_Resp(400, {"error": {"message": "max_tokens is required"}})])
    _install(monkeypatch, client)

    plain = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
    resp = await gateway.anthropic_messages(_request(plain), gw["user"])

    assert resp.status_code == 400
    assert len(client.sent) == 1


@pytest.mark.asyncio
async def test_stream_path_also_retries_and_does_not_crash_on_the_shadowed_name(gw, monkeypatch):
    """流式这条路有个陷阱: relay() 里把 `parsed` 重绑成了 SSE 的每一行, 它因此是
    relay 的**局部**变量 —— 在里面读外层那个 parsed 会 UnboundLocalError, 而那会
    在"已经答应给用户一个流"之后炸。所以请求体另起了 req_parsed。

    这条用例真跑一遍流式重试: 炸的话这里收到的是异常而不是重试。"""
    monkeypatch.setattr(gateway.config, "ANTHROPIC_PIN_THINKING_CHANNEL", False)
    client = _Client(
        [
            _Resp(400, {"error": {"message": "thinking.type.enabled is not supported"}}),
            _Resp(200, {"usage": {}}),
        ]
    )
    _install(monkeypatch, client)

    resp = await gateway.anthropic_messages(_request(_cc_body(stream=True)), gw["user"])
    chunks = [c async for c in resp.body_iterator]

    assert len(client.sent) == 2, "流式没重试"
    assert client.sent[1]["thinking"] == {"type": "adaptive"}
    body = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
    assert b"message_stop" in body, "重试成功后应当把真正的流转发下去"
    assert b"error" not in body
