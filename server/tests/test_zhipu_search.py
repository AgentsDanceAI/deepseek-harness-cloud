"""web_search via Zhipu: query extraction, result synthesis, and the gateway
endpoint returning the exact Anthropic shape dsh's mapAnthropicResponse parses."""

import asyncio
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-zhipu-")
os.environ.update(
    {
        "DHC_DEV": "1",
        "AUTH_SECRET": "test-secret",
        "DHC_DATA_DIR": _TMP,
        "DB_PATH": os.path.join(_TMP, "test.db"),
        "SEARCH_PROVIDER": "zhipu",
        "ZHIPU_SEARCH_API_KEY": "zhipu-test-key",
        "UPSTREAM_API_KEY": "sk-upstream-test",  # aligned across test modules: first import wins
        "FREE_SIGNUP_CREDITS": "500",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, credits, db, rate_limit, zhipu_search  # noqa: E402
from app.main import app  # noqa: E402

from ._signup import signup


@pytest.fixture(autouse=True)
def _zhipu_mode(monkeypatch):
    # config values freeze at first import (whichever test module wins the
    # race); pin the ones this file depends on so suite order never matters.
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "zhipu")
    monkeypatch.setattr(config, "ZHIPU_SEARCH_API_KEY", "zhipu-test-key")
    rate_limit._windows.clear()  # reset shared per-IP register cap across suite


def test_extract_query_strips_prefix():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Perform a web search for the query: rust async runtime"}
                ],
            }
        ]
    }
    assert zhipu_search.extract_query(body) == "rust async runtime"


def test_extract_query_plain_text_content():
    body = {"messages": [{"role": "user", "content": "just this"}]}
    assert zhipu_search.extract_query(body) == "just this"


def test_to_anthropic_response_shape():
    results = [
        {"url": "https://a.com", "title": "A", "content": "snippet A", "page_age": "2026-01-01"},
        {"url": "https://b.com", "title": "B", "content": "snippet B", "page_age": ""},
    ]
    resp = zhipu_search.to_anthropic_response("q", results, "deepseek-v4-flash")
    blocks = resp["content"]
    tool_result = [b for b in blocks if b["type"] == "web_search_tool_result"]
    assert len(tool_result) == 1
    items = tool_result[0]["content"]
    assert [i["url"] for i in items] == ["https://a.com", "https://b.com"]
    assert items[0]["title"] == "A" and items[0]["page_age"] == "2026-01-01"
    # snippets ride on a text block's citations, keyed by url
    text_blocks = [b for b in blocks if b["type"] == "text"]
    citations = text_blocks[0]["citations"]
    by_url = {c["url"]: c["cited_text"] for c in citations}
    assert by_url["https://a.com"] == "snippet A"


def test_to_anthropic_response_empty_still_has_result_block():
    # dsh raises if there's NO web_search_tool_result block; empty results must
    # still produce one (with zero items) so the search "succeeds with 0 hits".
    resp = zhipu_search.to_anthropic_response("q", [], "m")
    assert any(b["type"] == "web_search_tool_result" for b in resp["content"])


def _login() -> tuple[TestClient, str]:
    c = TestClient(app)
    signup(c, "z@test.local")
    uid = db.query_one("SELECT id FROM users WHERE email=?", ("z@test.local",))["id"]
    return c, uid


def test_gateway_search_endpoint_calls_zhipu(monkeypatch):
    c, uid = _login()
    before = credits.balance(uid)

    async def fake_search(query, count):
        assert query == "python gil removal"
        return [{"url": "https://x.com", "title": "X", "content": "c", "page_age": ""}]

    monkeypatch.setattr(zhipu_search, "search", fake_search)
    r = c.post(
        "/llm/anthropic/v1/messages",
        json={
            "model": "deepseek-v4-flash",
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Perform a web search for the query: python gil removal"}
                    ],
                }
            ],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        },
    )
    assert r.status_code == 200
    data = r.json()
    urls = [i["url"] for b in data["content"] if b["type"] == "web_search_tool_result" for i in b["content"]]
    assert urls == ["https://x.com"]
    # flat search fee charged, no upstream deepseek call
    assert before - credits.balance(uid) >= 1


def test_gateway_search_zhipu_failure_returns_empty(monkeypatch):
    c, _ = _login()

    async def boom(query, count):
        import httpx

        raise httpx.HTTPError("zhipu down")

    monkeypatch.setattr(zhipu_search, "search", boom)
    r = c.post(
        "/llm/anthropic/v1/messages",
        json={
            "model": "deepseek-v4-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Perform a web search for the query: anything"}],
                }
            ],
        },
    )
    # graceful: a valid (empty) search result, not a 5xx that dsh would surface as an error
    assert r.status_code == 200
    assert any(b["type"] == "web_search_tool_result" for b in r.json()["content"])


def test_gateway_does_not_bill_empty_search(monkeypatch):
    """A search that yields nothing is free: the agent retries on empty results,
    and charging each retry drained real balances for zero value."""
    c, uid = _login()

    async def empty(query, count):
        return []

    monkeypatch.setattr(zhipu_search, "search", empty)
    before = credits.balance(uid)
    r = c.post(
        "/llm/anthropic/v1/messages",
        json={
            "model": "deepseek-v4-flash",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Perform a web search for the query: nothing here"}],
                }
            ],
        },
    )
    assert r.status_code == 200
    assert credits.balance(uid) == before


def test_search_falls_back_when_engine_returns_linkless_rows(monkeypatch):
    """Zhipu's search_pro/search_std answer 200 with content but an empty link
    on every row; dsh discards url-less results, so we must fall through to an
    engine that carries links instead of reporting "no results"."""
    calls: list[str] = []

    async def fake_one(engine, query, count):
        calls.append(engine)
        if engine == "linkless":
            return []  # rows existed upstream but all lacked a url
        return [{"url": "https://ok.example/a", "title": "A", "content": "c", "page_age": ""}]

    monkeypatch.setattr(config, "ZHIPU_SEARCH_ENGINE", "linkless")
    monkeypatch.setattr(config, "ZHIPU_SEARCH_FALLBACKS", ["with_links"])
    monkeypatch.setattr(zhipu_search, "_search_one", fake_one)
    results = asyncio.run(zhipu_search.search("anything", 10))
    assert calls == ["linkless", "with_links"]
    assert [r["url"] for r in results] == ["https://ok.example/a"]


def test_search_skips_engine_that_errors(monkeypatch):
    """A per-engine quota rejection (429 余额不足) must not fail the whole search."""
    import httpx

    async def fake_one(engine, query, count):
        if engine == "broke":
            raise httpx.HTTPError("429 余额不足")
        return [{"url": "https://ok.example/b", "title": "B", "content": "c", "page_age": ""}]

    monkeypatch.setattr(config, "ZHIPU_SEARCH_ENGINE", "broke")
    monkeypatch.setattr(config, "ZHIPU_SEARCH_FALLBACKS", ["good"])
    monkeypatch.setattr(zhipu_search, "_search_one", fake_one)
    assert [r["url"] for r in asyncio.run(zhipu_search.search("q", 5))] == ["https://ok.example/b"]


def test_search_drops_linkless_rows(monkeypatch):
    """_search_one maps Zhipu rows and drops the url-less ones dsh cannot use."""

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "search_result": [
                    {"link": "", "title": "no link", "content": "x"},
                    {
                        "link": "https://ok.example/c",
                        "title": "ok",
                        "content": "y",
                        "publish_date": "2026-08-16",
                    },
                ]
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr(zhipu_search.httpx, "AsyncClient", lambda **kw: FakeClient())
    rows = asyncio.run(zhipu_search._search_one("any", "q", 10))
    assert [r["url"] for r in rows] == ["https://ok.example/c"]
    assert rows[0]["page_age"] == "2026-08-16"
