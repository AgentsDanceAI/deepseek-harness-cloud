"""web_search via Zhipu: query extraction, result synthesis, and the gateway
endpoint returning the exact Anthropic shape dsh's mapAnthropicResponse parses."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="dhc-zhipu-")
os.environ.update({
    "DHC_DEV": "1",
    "AUTH_SECRET": "test-secret",
    "DHC_DATA_DIR": _TMP,
    "DB_PATH": os.path.join(_TMP, "test.db"),
    "SEARCH_PROVIDER": "zhipu",
    "ZHIPU_SEARCH_API_KEY": "zhipu-test-key",
    "UPSTREAM_API_KEY": "sk-upstream-test",  # aligned across test modules: first import wins
    "FREE_SIGNUP_CREDITS": "500",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, credits, db, rate_limit, zhipu_search  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _zhipu_mode(monkeypatch):
    # config values freeze at first import (whichever test module wins the
    # race); pin the ones this file depends on so suite order never matters.
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "zhipu")
    monkeypatch.setattr(config, "ZHIPU_SEARCH_API_KEY", "zhipu-test-key")
    rate_limit._windows.clear()  # reset shared per-IP register cap across suite


def test_extract_query_strips_prefix():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Perform a web search for the query: rust async runtime"}]}]}
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
    r = c.post("/api/auth/register", json={"email": "z@test.local", "password": "password123"})
    if r.status_code == 409:  # already registered by an earlier test in this process
        c.post("/api/auth/login", json={"email": "z@test.local", "password": "password123"})
    uid = db.query_one("SELECT id FROM users WHERE email=?", ("z@test.local",))["id"]
    return c, uid


def test_gateway_search_endpoint_calls_zhipu(monkeypatch):
    c, uid = _login()
    before = credits.balance(uid)

    async def fake_search(query, count):
        assert query == "python gil removal"
        return [{"url": "https://x.com", "title": "X", "content": "c", "page_age": ""}]

    monkeypatch.setattr(zhipu_search, "search", fake_search)
    r = c.post("/llm/anthropic/v1/messages", json={
        "model": "deepseek-v4-flash", "max_tokens": 4096,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Perform a web search for the query: python gil removal"}]}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
    })
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
    r = c.post("/llm/anthropic/v1/messages", json={
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Perform a web search for the query: anything"}]}],
    })
    # graceful: a valid (empty) search result, not a 5xx that dsh would surface as an error
    assert r.status_code == 200
    assert any(b["type"] == "web_search_tool_result" for b in r.json()["content"])
