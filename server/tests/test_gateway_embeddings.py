"""POST /llm/v1/embeddings —— 知识库的向量化入口。

这条路上的错法几乎全是**静默**的: 计价走错口径只是账单上多一个数字, 目录漏
一道检查只是上游回一句看不懂的 500, 而向量化是批量调用 —— 等有人发现时已经
错了几万条。所以这里钉的是"不报错的那些", 不是 happy path。
"""

from __future__ import annotations

import json
import math
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="dhc-embeddings-")
os.environ.setdefault("DHC_DEV", "1")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("DHC_DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "test.db"))
os.environ.setdefault("UPSTREAM_API_KEY", "sk-upstream-test")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, credits, db, gateway, model_catalog, plans, rate_limit  # noqa: E402
from app.main import app  # noqa: E402

from ._signup import signup  # noqa: E402

URL = "/llm/v1/embeddings"


class _FakeUpstream:
    """gateway._upstream_client 的替身。记下请求, 好断言"根本没调上游"。"""

    calls: list[dict] = []

    def __init__(self, *, json_body=None, status=200):
        self._json = json_body
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, url, **kw):
        _FakeUpstream.calls.append({"url": url, **kw})
        return httpx.Response(self._status, json=self._json, request=httpx.Request("POST", url))


def _vec_response(prompt_tokens: int | None, n: int = 1) -> dict:
    body = {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2]} for i in range(n)],
        "model": "whatever-upstream-says",
    }
    if prompt_tokens is not None:
        body["usage"] = {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens}
    return body


@pytest.fixture(autouse=True)
def _pin_upstream(monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-upstream-test")
    monkeypatch.setattr(config, "UPSTREAM_BASE_URL", "https://api.qianmian.ai/v1")
    # QPS 桶是**进程级**的, 而本模块所有用例共用一个账号 —— 攒到第 16 发就开始
    # 撞 429, 于是"加一条用例"会让**另一条**用例红, 而错误看着与它毫无关系。
    # 每个用例发一个新桶, 限流本身另有用例专管。
    monkeypatch.setattr(gateway, "_qps", rate_limit.TokenBucket(config.GATEWAY_QPS, config.GATEWAY_QPS_BURST))
    _FakeUpstream.calls = []


@pytest.fixture()
def user():
    client = TestClient(app)
    email = "embed@test.local"
    signup(client, email)
    return client, db.query_one("SELECT id FROM users WHERE email=?", (email,))["id"]


def _stub(monkeypatch, **kw):
    monkeypatch.setattr(gateway, "_upstream_client", lambda: _FakeUpstream(**kw))


def _last_usage(uid: str):
    return db.query_one("SELECT * FROM usage_log WHERE user_id=? ORDER BY created DESC", (uid,))


def _usage_count(uid: str) -> int:
    # 同一个 fixture 邮箱 = 同一个用户, 用例之间账单是**累积**的; "没记账"只能
    # 用条数没变来断言, 不能用"没有任何一条"。
    return int(db.query_one("SELECT COUNT(*) AS n FROM usage_log WHERE user_id=?", (uid,))["n"])


def _default_id() -> str:
    return model_catalog.default_embedding_model()


# --- 目录 --------------------------------------------------------------------


def test_embeddings_requires_auth():
    assert TestClient(app).post(URL, json={"input": "hi"}).status_code == 401


def test_catalog_has_a_usable_default():
    """默认必须真的在目录里, 而且维度是个正数 —— Coze 拿这两个值去建向量集合。"""
    entry = model_catalog.resolve_embedding(_default_id())
    assert entry is not None, "models.json 缺 embedding_models 或没有 default"
    assert isinstance(entry["dimensions"], int) and entry["dimensions"] > 0


def test_embedding_models_are_not_offered_as_chat_models(user):
    """向量化模型混进 /v1/models 就等于摆进 dsh 的"可以聊天的模型"下拉里。

    选中它不会报"这不是对话模型", 只会得到一次上游错误 —— 用户只看见对话坏了。
    """
    client, _ = user
    listed = {m["id"] for m in client.get("/llm/v1/models").json()["data"]}
    assert listed.isdisjoint(model_catalog.embedding_catalog())


def test_chat_model_id_is_rejected(monkeypatch, user):
    """反向那道: 拿对话模型的 id 来做向量化, 必须 404 且**不碰上游**。"""
    client, uid = user
    _stub(monkeypatch, json_body=_vec_response(10))
    before = credits.balance(uid)
    r = client.post(URL, json={"model": model_catalog.default_model(), "input": "hi"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
    assert _FakeUpstream.calls == []
    assert credits.balance(uid) == before


def test_unknown_model_is_rejected(monkeypatch, user):
    client, _ = user
    _stub(monkeypatch, json_body=_vec_response(10))
    r = client.post(URL, json={"model": "no-such-embedder", "input": "hi"})
    assert r.status_code == 404
    assert _FakeUpstream.calls == []


# --- 计价 --------------------------------------------------------------------


def test_bills_input_tokens_only(monkeypatch, user):
    client, uid = user
    _stub(monkeypatch, json_body=_vec_response(1000))
    before = credits.balance(uid)
    r = client.post(URL, json={"model": _default_id(), "input": ["a", "b"]})
    assert r.status_code == 200
    row = _last_usage(uid)
    assert row["kind"] == "embedding"
    assert row["model"] == _default_id()
    assert (row["uncached_input"], row["cache_read"], row["output"]) == (1000, 0, 0)
    assert row["credits"] == model_catalog.charge_embedding_credits(_default_id(), 1000)
    assert before - credits.balance(uid) == row["credits"]


def test_not_billed_at_chat_catalog_prices(monkeypatch, user):
    """这条钉的是**用错函数**。

    charge_credits 认的是对话目录, 向量化的 id 不在里面 —— 它会走"最贵条目"
    兜底, 于是一次知识库导入按最贵对话模型的价钱结账。账单上只是一个大一点的
    数字, 没有任何报错。把 gateway 里的 charge_embedding_credits 换成
    charge_credits, 这条必须红。
    """
    client, uid = user
    _stub(monkeypatch, json_body=_vec_response(1_000_000))
    client.post(URL, json={"model": _default_id(), "input": "x"})
    charged = _last_usage(uid)["credits"]
    assert charged == model_catalog.charge_embedding_credits(_default_id(), 1_000_000)
    assert charged < model_catalog.charge_credits(_default_id(), 1_000_000, 0, 0)


def test_free_upstream_model_still_costs_one_credit():
    """上游 0 元的型号 (BGE-M3) 不能变成白嫖口子: 有 token 流动就至少 1 积分。"""
    free = [m for m in model_catalog.embedding_catalog().values() if not m["input_usd_per_m"]]
    if not free:
        pytest.skip("目录里当前没有 0 元型号")
    assert model_catalog.charge_embedding_credits(free[0]["id"], 1) >= 1
    assert model_catalog.charge_embedding_credits(free[0]["id"], 0) == 0


def test_missing_usage_is_estimated_not_free(monkeypatch, user):
    """上游漏了 usage 不能变成免单 —— 按送上去的 input 字节估。"""
    client, uid = user
    _stub(monkeypatch, json_body=_vec_response(None))
    text = ["chunk " * 200, "另一段" * 200]
    r = client.post(URL, json={"model": _default_id(), "input": text})
    assert r.status_code == 200
    sent = len(json.dumps(text, ensure_ascii=False).encode())
    row = _last_usage(uid)
    assert row["uncached_input"] == math.ceil(sent / gateway.STREAM_FALLBACK_BYTES_PER_TOKEN)
    assert row["credits"] >= 1


def test_upstream_error_is_not_billed(monkeypatch, user):
    client, uid = user
    _stub(monkeypatch, json_body={"error": {"message": "boom"}}, status=500)
    before, rows = credits.balance(uid), _usage_count(uid)
    r = client.post(URL, json={"model": _default_id(), "input": "hi"})
    assert r.status_code == 502
    assert credits.balance(uid) == before
    assert _usage_count(uid) == rows


def test_upstream_auth_failure_does_not_surface_as_401(monkeypatch, user):
    """我们自己的 key 被上游拒了, 不能表现成 401 —— 客户端会去怪用户的令牌。"""
    client, _ = user
    _stub(monkeypatch, json_body={"error": {"message": "bad key"}}, status=401)
    assert client.post(URL, json={"model": _default_id(), "input": "hi"}).status_code == 502


# --- 请求整形 ----------------------------------------------------------------


def test_upstream_gets_our_key_and_the_upstream_model_id(monkeypatch, user):
    client, _ = user
    _stub(monkeypatch, json_body=_vec_response(3))
    assert client.post(URL, json={"input": "hi"}).status_code == 200  # 不给 model 走默认
    call = _FakeUpstream.calls[-1]
    assert call["url"].endswith("/embeddings")
    assert call["headers"]["authorization"] == "Bearer sk-upstream-test"
    entry = model_catalog.resolve_embedding(_default_id())
    assert call["json"]["model"] == entry.get("upstream_model", _default_id())


def test_dimensions_on_a_model_that_refuses_it_fails_loudly(monkeypatch, user):
    """悄悄丢掉 dimensions 是最坏的一种: 客户端按 512 维建了集合, 拿回 1024 维,
    错要等到写向量库那一刻才炸, 而那时错的是"写入", 不是"参数"。"""
    picky = [m for m in model_catalog.embedding_catalog().values() if not m["supports_dimensions"]]
    if not picky:
        pytest.skip("目录里当前没有拒收 dimensions 的型号")
    client, _ = user
    _stub(monkeypatch, json_body=_vec_response(3))
    r = client.post(URL, json={"model": picky[0]["id"], "input": "hi", "dimensions": 512})
    assert r.status_code == 400
    assert _FakeUpstream.calls == []


def test_dimensions_passes_through_when_supported(monkeypatch, user):
    client, _ = user
    _stub(monkeypatch, json_body=_vec_response(3))
    r = client.post(URL, json={"model": _default_id(), "input": "hi", "dimensions": 512})
    assert r.status_code == 200
    assert _FakeUpstream.calls[-1]["json"]["dimensions"] == 512


def test_response_is_relayed_verbatim(monkeypatch, user):
    """报文原样回, 不重新组装。

    langchain / Dify 的 OpenAI 客户端**默认**发 encoding_format=base64, 拿回来的
    `embedding` 是一个 base64 字符串而不是数组。任何"顺手规整一下响应"的改动都会
    把它弄坏, 而坏法是客户端那边解出一堆乱数 —— 检索结果变差, 没有任何报错。
    2026-08-29 线上实测过这条路是通的, 这里把它钉住。
    """
    client, _ = user
    upstream = {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": "c29tZS1iYXNlNjQ="}],
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }
    _stub(monkeypatch, json_body=upstream)
    r = client.post(URL, json={"input": "hi", "encoding_format": "base64"})
    assert r.status_code == 200
    assert r.json() == upstream
    # 客户端要的编码必须原样送到上游, 否则它拿回数组、按 base64 去解。
    assert _FakeUpstream.calls[-1]["json"]["encoding_format"] == "base64"


def test_usage_falls_back_to_total_tokens(monkeypatch, user):
    """有的型号只给 total_tokens 不给 prompt_tokens (线上 text-embedding-3-small
    就是), 少了这一档就会走字节估算 —— 不是免单, 但账不准。"""
    client, uid = user
    _stub(
        monkeypatch,
        json_body={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "usage": {"total_tokens": 777},
        },
    )
    assert client.post(URL, json={"input": "hi"}).status_code == 200
    assert _last_usage(uid)["uncached_input"] == 777


@pytest.mark.parametrize("body", [{"input": ""}, {"input": []}, {}])
def test_empty_input_is_rejected_without_touching_upstream(monkeypatch, user, body):
    client, uid = user
    _stub(monkeypatch, json_body=_vec_response(3))
    rows = _usage_count(uid)
    r = client.post(URL, json=body)
    assert r.status_code == 400
    assert _FakeUpstream.calls == []
    assert _usage_count(uid) == rows


def test_non_json_body_is_rejected(monkeypatch, user):
    client, _ = user
    _stub(monkeypatch, json_body=_vec_response(3))
    r = client.post(URL, content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert _FakeUpstream.calls == []


# --- 准入 --------------------------------------------------------------------


def test_no_credits_blocks_before_the_upstream_call(monkeypatch, user):
    """余额耗尽时向量化和聊天同一道闸: 402 + insufficient_quota, 且不打上游。"""
    client, _ = user
    monkeypatch.setattr(plans, "check_run_blocked", lambda uid: "no_credits")
    _stub(monkeypatch, json_body=_vec_response(3))
    r = client.post(URL, json={"input": "hi"})
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "insufficient_quota"
    assert _FakeUpstream.calls == []


def test_gateway_without_upstream_key_is_unconfigured(monkeypatch, user):
    client, _ = user
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "")
    r = client.post(URL, json={"input": "hi"})
    assert r.status_code == 503
