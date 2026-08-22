"""Translate dsh's Anthropic-Messages web_search into a Zhipu web_search call.

dsh's web-search-deepseek provider POSTs to {base}/messages:
    { model, max_tokens,
      messages: [{role:"user", content:[{type:"text",
                  text:"Perform a web search for the query: <QUERY>"}]}],
      tools: [{type:"web_search_20250305", name:"web_search", max_uses:N}] }
and parses the response by walking content[] for a `web_search_tool_result`
block whose items are {type:"web_search_result", url, title?, page_age?};
snippets come from `text` blocks' citations[] keyed by url with cited_text.
If no web_search_tool_result block is present it raises an error.

So we run the query through Zhipu (open.bigmodel.cn) and synthesize exactly
that shape: one web_search_tool_result block + one text block whose citations
carry each result's snippet. This keeps the provider adaptation behind the
gateway's Anthropic-compatible surface.
"""

from __future__ import annotations

import re

import httpx

from . import config

_QUERY_PREFIX = re.compile(r"^\s*perform a web search for the query:\s*", re.IGNORECASE)


def extract_query(body: dict) -> str:
    """Pull the search query out of the Anthropic Messages request."""
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = " ".join(
                block.get("text", "")
                for block in (content or [])
                if isinstance(block, dict) and block.get("type") == "text"
            )
        text = _QUERY_PREFIX.sub("", text).strip()
        if text:
            return text
    return ""


def _max_results(body: dict) -> int:
    for tool in body.get("tools", []):
        if isinstance(tool, dict) and tool.get("name") == "web_search":
            try:
                return max(1, min(int(tool.get("max_uses", 5)) * 2, 20))
            except (TypeError, ValueError):
                return 10
    return 10


async def _search_one(engine: str, query: str, count: int) -> list[dict]:
    """One Zhipu call against a named engine; maps its rows to our shape."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        r = await http.post(
            f"{config.ZHIPU_SEARCH_BASE.rstrip('/')}/web_search",
            headers={"Authorization": f"Bearer {config.ZHIPU_SEARCH_API_KEY}"},
            json={
                "search_query": query[:70],
                "search_engine": engine,
                "search_intent": False,
                "count": max(1, min(int(count or 10), 50)),
                "content_size": "medium",
            },
        )
        r.raise_for_status()
        data = r.json()
    out = []
    for item in data.get("search_result") or []:
        # dsh's parser drops any web_search_result whose url is empty and dedupes
        # by url, so a link-less row is worthless downstream — skip it here and
        # let the caller fall back to an engine that carries links.
        url = str(item.get("link") or "").strip()
        if not url:
            continue
        out.append(
            {
                "url": url,
                "title": str(item.get("title") or "").strip(),
                "content": str(item.get("content") or "").strip(),
                "page_age": str(item.get("publish_date") or "").strip(),
            }
        )
    return out


async def search(query: str, count: int) -> list[dict]:
    """Call Zhipu web_search. Returns [{title, url, content, page_age}]. []=no results.

    Engines are tried in order because some can return rows without links. dsh
    requires a non-empty URL, so link-less rows are unusable. The configured
    engine and fallbacks are all attempted until one returns valid results.
    """
    if not config.ZHIPU_SEARCH_API_KEY or not query.strip():
        return []
    engines: list[str] = []
    for engine in (config.ZHIPU_SEARCH_ENGINE, *config.ZHIPU_SEARCH_FALLBACKS):
        engine = (engine or "").strip()
        if engine and engine not in engines:
            engines.append(engine)
    for engine in engines:
        try:
            results = await _search_one(engine, query, count)
        except (httpx.HTTPError, ValueError):
            continue  # e.g. 429 "余额不足" on one engine's quota — try the next
        if results:
            return results
    return []


def to_anthropic_response(query: str, results: list[dict], model: str) -> dict:
    """Build the exact Anthropic Messages shape dsh's mapAnthropicResponse parses:
    a web_search_tool_result block (url/title/page_age) plus a text block whose
    citations carry each result's snippet keyed by url."""
    search_items = []
    citations = []
    for item in results:
        entry = {"type": "web_search_result", "url": item["url"]}
        if item.get("title"):
            entry["title"] = item["title"]
        if item.get("page_age"):
            entry["page_age"] = item["page_age"]
        search_items.append(entry)
        if item.get("content"):
            citations.append(
                {
                    "type": "web_search_result_location",
                    "url": item["url"],
                    "title": item.get("title", ""),
                    "cited_text": item["content"][:500],
                }
            )

    content = [
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_zhipu",
            "content": search_items,
        }
    ]
    # A text block carrying citations is where dsh reads snippets from.
    summary = f"Found {len(results)} results for: {query}" if results else f"No results for: {query}"
    text_block: dict = {"type": "text", "text": summary}
    if citations:
        text_block["citations"] = citations
    content.append(text_block)

    return {
        "id": "msg_zhipu_search",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
