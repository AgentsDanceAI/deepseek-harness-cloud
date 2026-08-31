"""The LLM gateway. The upstream API key lives ONLY here, server-side.

The chat and Anthropic surfaces match exactly what dsh emits; /v1/embeddings
is for the cloud workspaces' knowledge bases, which speak OpenAI too:

  POST /llm/v1/chat/completions   OpenAI-compatible chat completions (llm-deepseek
                                  adapter; always stream:true + include_usage)
  POST /llm/v1/embeddings         OpenAI-compatible embeddings (knowledge bases in
                                  the cloud workspaces: Coze / Dify / RAGFlow)
  POST /llm/anthropic/v1/messages Anthropic Messages (web-search-deepseek hits
                                  {base}/messages with x-api-key + anthropic-version)
  GET  /llm/v1/models             catalog listing (pi-ai discovery compatible)

Admission order: token -> account -> concurrency -> QPS -> credits. Once a
request is admitted its stream is never cut; usage is billed truthfully at the
end (small overdraft possible, gates close afterwards).

Error contract (verified against dsh's adapter): 401/403 -> AUTH (no retry),
429 -> RATE_LIMIT, 402 + insufficient_quota body -> QUOTA_EXCEEDED (no retry).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, credits, model_catalog, plans, rate_limit, zhipu_search
from .accounts import resolve_user
from .http_limits import read_limited_body

router = APIRouter(prefix="/llm", tags=["gateway"])
log = logging.getLogger("dhc.gateway")
STREAM_FALLBACK_BYTES_PER_TOKEN = 4

_qps = rate_limit.TokenBucket(config.GATEWAY_QPS, config.GATEWAY_QPS_BURST)

# In-flight request counter per user (single-process; use Redis for multi-worker).
_inflight: dict[str, int] = {}
_inflight_lock = threading.Lock()


class _Slot:
    def __init__(self, user_id: str):
        self.user_id = user_id

    def __enter__(self):
        with _inflight_lock:
            _inflight[self.user_id] = _inflight.get(self.user_id, 0) + 1
        return self

    def __exit__(self, *_):
        with _inflight_lock:
            _inflight[self.user_id] = max(0, _inflight.get(self.user_id, 1) - 1)
        return False


def _openai_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": code, "code": code}}
    )


# One agent "task" is a long main stream plus short auxiliary model calls dsh
# fires alongside it (session titling, compaction). The plan's concurrency
# number counts TASKS, so the gateway allows that many streams plus headroom
# for the auxiliaries — without this, free-tier (concurrency 1) deadlocks
# against its own title request and every chat looks dead.
AUX_REQUEST_HEADROOM = 2


def _admit(user: dict) -> JSONResponse | None:
    """Returns an error response if the request must be rejected, else None."""
    uid = user["id"]
    limit = plans.concurrency_limit(uid) + AUX_REQUEST_HEADROOM
    if _inflight.get(uid, 0) >= limit:
        return _openai_error(
            429,
            "concurrency_limit",
            "Too many simultaneous requests for the current plan. "
            "Wait for running tasks or upgrade for more concurrency.",
        )
    if not _qps.take(uid):
        return _openai_error(429, "rate_limit_exceeded", "Too many requests, slow down.")
    reason = plans.check_run_blocked(uid)
    if reason:
        # 这段文案会**原样显示在客户端的聊天窗口里** —— 它是用户在付费转化那一刻
        # 唯一看到的东西, 所以要说人话、要给出下一步。客户端目前无法把它渲染成
        # 带按钮的卡片 (dsh 的 LLM seam 没有暴露可监听的失败事件), 所以链接必须
        # 写在正文里让人能复制。
        #
        # 两种阻断的处置**完全不同**, 不能压成同一句: 余额耗尽是自己充值就能解,
        # 而团队成员额度上限要找管理员调 —— 告诉后者"去充值"是误导, 他充了也没用。
        if reason == "member_cap_reached":
            message = (
                "你在团队共享额度中的个人上限已用完。请联系团队管理员调高你的额度上限"
                f"（管理员可在 {config.PUBLIC_BASE}/team 调整）。"
            )
        else:
            message = (
                f"账户余额已用完，无法继续。前往 {config.PUBLIC_BASE}/pricing 充值或升级套餐后即可恢复。"
            )
        return _openai_error(402, "insufficient_quota", message)
    return None


def _upstream_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(config.UPSTREAM_TIMEOUT_S, connect=15.0))


def _require_upstream() -> None:
    if not config.UPSTREAM_API_KEY:
        raise HTTPException(503, "gateway_not_configured")


# --- OpenAI-compatible chat completions -------------------------------------


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, user: dict = Depends(resolve_user)):
    _require_upstream()
    rejected = _admit(user)
    if rejected is not None:
        return rejected

    try:
        raw = await read_limited_body(
            request,
            max_bytes=config.GATEWAY_BODY_MAX_BYTES,
            timeout_s=config.REQUEST_BODY_TIMEOUT_S,
        )
        body = json.loads(raw)
    except json.JSONDecodeError:
        return _openai_error(400, "invalid_request_error", "Body must be JSON.")

    model_id = str(body.get("model", "")) or model_catalog.default_model()
    entry = model_catalog.resolve(model_id)
    if entry is None:
        return _openai_error(
            404, "model_not_found", f"Model '{model_id}' is not offered. See GET /llm/v1/models."
        )
    body["model"] = entry.get("upstream_model", model_id)
    stream = bool(body.get("stream", False))
    if stream:
        # Guarantee a usage chunk even if a non-dsh client forgot to ask.
        opts = body.get("stream_options") or {}
        opts["include_usage"] = True
        body["stream_options"] = opts

    request_id = f"dhc-{uuid.uuid4().hex[:16]}"
    headers = {
        "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
    }
    url = config.UPSTREAM_BASE_URL.rstrip("/") + "/chat/completions"

    def bill(usage: dict | None, forwarded_bytes: int = 0) -> None:
        u = dict(usage or {})
        if u.get("prompt_tokens") is None:
            prompt_bytes = len(json.dumps(body, ensure_ascii=False).encode())
            u["prompt_tokens"] = max(1, math.ceil(prompt_bytes / STREAM_FALLBACK_BYTES_PER_TOKEN))
        if u.get("completion_tokens") is None:
            u["completion_tokens"] = max(1, math.ceil(forwarded_bytes / STREAM_FALLBACK_BYTES_PER_TOKEN))
        cache_read = int(
            u.get("prompt_cache_hit_tokens")
            or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
        prompt = int(u.get("prompt_tokens") or 0)
        uncached = max(0, prompt - cache_read)
        output = int(u.get("completion_tokens") or 0)
        amount = model_catalog.charge_credits(model_id, uncached, cache_read, output)
        credits.spend(
            user["id"],
            amount,
            kind="llm",
            model=model_id,
            device_id=user.get("device_id", ""),
            uncached_input=uncached,
            cache_read=cache_read,
            output=output,
            request_id=request_id,
        )

    if not stream:
        async with _upstream_client() as client:
            with _Slot(user["id"]):
                upstream = await client.post(url, json=body, headers=headers)
            if upstream.status_code == 200:
                data = upstream.json()
                bill(data.get("usage"))
                return JSONResponse(content=data, headers={"x-request-id": request_id})
            return _relay_upstream_error(upstream, request_id, body)

    async def relay():
        slot = _Slot(user["id"])
        slot.__enter__()
        usage: dict | None = None
        forwarded_bytes = 0
        upstream_started = False
        stream_exhausted = False
        buffer = b""
        try:
            async with _upstream_client() as client:
                async with client.stream("POST", url, json=body, headers=headers) as upstream:
                    if not 200 <= upstream.status_code < 300:
                        detail = await upstream.aread()
                        yield _sse_error_bytes(upstream.status_code, detail)
                        return
                    upstream_started = True
                    async for chunk in upstream.aiter_raw():
                        forwarded_bytes += len(chunk)
                        # forward verbatim; scan complete lines for the usage chunk
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            text = line.strip()
                            if text.startswith(b"data:") and b'"usage"' in text:
                                try:
                                    parsed = json.loads(text[5:].strip())
                                    if isinstance(parsed.get("usage"), dict):
                                        usage = parsed["usage"]
                                except (json.JSONDecodeError, AttributeError):
                                    pass
                        yield chunk
                    stream_exhausted = True
        finally:
            slot.__exit__()
            if upstream_started and (forwarded_bytes or not stream_exhausted):
                try:
                    bill(usage, forwarded_bytes)
                except Exception:
                    log.exception("failed to bill OpenAI stream request_id=%s", request_id)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"x-request-id": request_id, "cache-control": "no-cache"},
    )


# --- OpenAI Responses API ----------------------------------------------------
#
# Codex CLI 只认这一面: 它从某个版本起**不再支持** wire_api="chat"
# (`\`wire_api = "chat"\` is no longer supported`)。上游千面原生就有 /v1/responses,
# 所以这里是透传 + 计量, 不是翻译层。
#
# 计量口径与 chat 面一致, 只是字段名不同: Responses 用 input_tokens/output_tokens
# (缓存命中在 input_tokens_details.cached_tokens), chat 用 prompt/completion。
@router.post("/v1/responses")
async def responses(request: Request, user: dict = Depends(resolve_user)):
    _require_upstream()
    rejected = _admit(user)
    if rejected is not None:
        return rejected

    try:
        raw = await read_limited_body(
            request,
            max_bytes=config.GATEWAY_BODY_MAX_BYTES,
            timeout_s=config.REQUEST_BODY_TIMEOUT_S,
        )
        body = json.loads(raw)
    except json.JSONDecodeError:
        return _openai_error(400, "invalid_request_error", "Body must be JSON.")
    if not isinstance(body, dict):
        return _openai_error(400, "invalid_request_error", "Body must be a JSON object.")

    model_id = str(body.get("model", "")) or model_catalog.default_model()
    entry = model_catalog.resolve(model_id)
    if entry is None:
        return _openai_error(
            404, "model_not_found", f"Model '{model_id}' is not offered. See GET /llm/v1/models."
        )
    body["model"] = entry.get("upstream_model", model_id)
    stream = bool(body.get("stream", False))

    request_id = f"dhc-{uuid.uuid4().hex[:16]}"
    headers = {
        "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
        "content-type": "application/json",
        "accept": "text/event-stream" if stream else "application/json",
    }
    url = config.UPSTREAM_BASE_URL.rstrip("/") + "/responses"

    def bill(usage: dict | None, forwarded_bytes: int = 0) -> None:
        u = dict(usage or {})
        if u.get("input_tokens") is None:
            prompt_bytes = len(json.dumps(body, ensure_ascii=False).encode())
            u["input_tokens"] = max(1, math.ceil(prompt_bytes / STREAM_FALLBACK_BYTES_PER_TOKEN))
        if u.get("output_tokens") is None:
            u["output_tokens"] = max(1, math.ceil(forwarded_bytes / STREAM_FALLBACK_BYTES_PER_TOKEN))
        cache_read = int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        prompt = int(u.get("input_tokens") or 0)
        uncached = max(0, prompt - cache_read)
        output = int(u.get("output_tokens") or 0)
        credits.spend(
            user["id"],
            model_catalog.charge_credits(model_id, uncached, cache_read, output),
            kind="llm",
            model=model_id,
            device_id=user.get("device_id", ""),
            uncached_input=uncached,
            cache_read=cache_read,
            output=output,
            request_id=request_id,
        )

    if not stream:
        async with _upstream_client() as client:
            with _Slot(user["id"]):
                upstream = await client.post(url, json=body, headers=headers)
            if upstream.status_code == 200:
                data = upstream.json()
                bill(data.get("usage"))
                return JSONResponse(content=data, headers={"x-request-id": request_id})
            return _relay_upstream_error(upstream, request_id, body)

    async def relay():
        slot = _Slot(user["id"])
        slot.__enter__()
        usage: dict | None = None
        forwarded_bytes = 0
        upstream_started = False
        stream_exhausted = False
        buffer = b""
        try:
            async with _upstream_client() as client:
                async with client.stream("POST", url, json=body, headers=headers) as upstream:
                    if not 200 <= upstream.status_code < 300:
                        detail = await upstream.aread()
                        yield _sse_error_bytes(upstream.status_code, detail)
                        return
                    upstream_started = True
                    async for chunk in upstream.aiter_raw():
                        forwarded_bytes += len(chunk)
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            text = line.strip()
                            if text.startswith(b"data:") and b'"usage"' in text:
                                try:
                                    parsed = json.loads(text[5:].strip())
                                except (json.JSONDecodeError, AttributeError):
                                    continue
                                # 用量挂在收尾事件的 response 里 (response.completed),
                                # 少数实现直接挂顶层 —— 两处都认, 认错就是白送。
                                for cand in (
                                    parsed.get("usage"),
                                    (parsed.get("response") or {}).get("usage"),
                                ):
                                    if isinstance(cand, dict):
                                        usage = cand
                        yield chunk
                    stream_exhausted = True
        finally:
            slot.__exit__()
            if upstream_started and (forwarded_bytes or not stream_exhausted):
                try:
                    bill(usage, forwarded_bytes)
                except Exception:
                    log.exception("failed to bill Responses stream request_id=%s", request_id)

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"x-request-id": request_id, "cache-control": "no-cache"},
    )


# --- OpenAI-compatible embeddings -------------------------------------------
#
# 知识库要向量化。Coze / Dify / RAGFlow 全都只会说 OpenAI 的这一支, 缺了它,
# 用户得自己去第三方申请一把 key —— 而他已经在我们这里付过费了。
#
# 上游走的是同一个千面网关 (2026-08-29 实测 POST /v1/embeddings 通, 返回标准
# OpenAI 形状 + usage.prompt_tokens)。**不直连百炼**: 隐私政策的服务商表里
# 「阿里巴巴」是记在「经千面网关的上游」那一行的, 直连会新增一个直接接收方,
# 中英双语政策都得改; 走千面一个字都不用动。


@router.post("/v1/embeddings")
async def embeddings(request: Request, user: dict = Depends(resolve_user)):
    _require_upstream()
    rejected = _admit(user)
    if rejected is not None:
        return rejected

    try:
        raw = await read_limited_body(
            request,
            max_bytes=config.GATEWAY_BODY_MAX_BYTES,
            timeout_s=config.REQUEST_BODY_TIMEOUT_S,
        )
        body = json.loads(raw)
    except json.JSONDecodeError:
        return _openai_error(400, "invalid_request_error", "Body must be JSON.")
    if not isinstance(body, dict):
        return _openai_error(400, "invalid_request_error", "Body must be a JSON object.")

    model_id = str(body.get("model", "")) or model_catalog.default_embedding_model()
    entry = model_catalog.resolve_embedding(model_id)
    if entry is None:
        # 对话模型的 id 也走这条分支。放行的话上游会拿一个不会做向量的模型去做
        # 向量, 而计费要么按最贵条目兜底、要么按对话价 —— 两种都是静默收错钱。
        return _openai_error(
            404,
            "model_not_found",
            f"Model '{model_id}' is not offered for embeddings. "
            f"Available: {', '.join(model_catalog.embedding_catalog())}.",
        )

    text = body.get("input")
    if text is None or (isinstance(text, (str, list)) and len(text) == 0):
        return _openai_error(400, "invalid_request_error", "input is required.")
    if body.get("dimensions") is not None and not entry.get("supports_dimensions"):
        # 上游对这个会回 400 + 一句它自己的 "The parameter is invalid", 看不出是
        # 哪个参数。悄悄把 dimensions 丢掉更糟: 客户端按自己要的维度建了集合,
        # 拿回来的却是原生维度, 报错要等到写向量库那一刻。
        return _openai_error(
            400,
            "invalid_request_error",
            f"Model '{model_id}' does not accept 'dimensions'; it always returns "
            f"{entry.get('dimensions')} dimensions.",
        )

    body["model"] = entry.get("upstream_model", model_id)
    request_id = f"dhc-{uuid.uuid4().hex[:16]}"
    headers = {
        "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
        "content-type": "application/json",
        "accept": "application/json",
    }
    url = config.UPSTREAM_BASE_URL.rstrip("/") + "/embeddings"

    async with _upstream_client() as client:
        with _Slot(user["id"]):
            upstream = await client.post(url, json=body, headers=headers)
    if upstream.status_code != 200:
        # 上游报错的路径上一分不收 —— 与聊天、与生图同口径。
        return _relay_upstream_error(upstream, request_id, body)

    data = upstream.json()
    usage = data.get("usage") or {}
    tokens = usage.get("prompt_tokens")
    if tokens is None:
        tokens = usage.get("total_tokens")
    if tokens is None:
        # 上游漏了 usage 不能变成免单: 按送上去的 input 字节估, 与流式聊天断流时
        # 同一个兜底系数。
        sent = len(json.dumps(text, ensure_ascii=False).encode())
        tokens = max(1, math.ceil(sent / STREAM_FALLBACK_BYTES_PER_TOKEN))
    tokens = int(tokens)
    amount = model_catalog.charge_embedding_credits(model_id, tokens)
    credits.spend(
        user["id"],
        amount,
        kind="embedding",
        model=model_id,
        device_id=user.get("device_id", ""),
        uncached_input=tokens,
        request_id=request_id,
    )
    return JSONResponse(content=data, headers={"x-request-id": request_id})


def _body_shape(body: object) -> str:
    """请求的**形状**摘要, 用来诊断上游 4xx。只记结构, 不记内容。

    上游拒一个请求时只会说一句它自己的话 (比如 "The content field is a required
    field."), 而我们这边**看不见自己发出去的是什么** —— 于是同样的报错在
    Coze/Dify/任意客户端上都长一个样, 只能靠猜。2026-08-31 Coze 的工作流撞上
    这个 400, 排查时才发现整条路上一点线索都没有。

    只记角色和字段名: 消息正文是用户数据, 不进日志。
    """
    if not isinstance(body, dict):
        return type(body).__name__
    msgs = body.get("messages")
    parts = [f"model={body.get('model')!r}"]
    if isinstance(msgs, list):
        shapes = []
        for m in msgs[:12]:
            if not isinstance(m, dict):
                shapes.append(type(m).__name__)
                continue
            keys = sorted(k for k in m if k != "content")
            c = m.get("content")
            ctype = (
                "missing"
                if "content" not in m
                else "null"
                if c is None
                else f"list[{len(c)}]"
                if isinstance(c, list)
                else "empty"
                if c == ""
                else "str"
            )
            shapes.append(f"{m.get('role')}:content={ctype}{'+' + ','.join(keys) if keys else ''}")
        parts.append("messages=[" + " | ".join(shapes) + "]")
    for k in ("tools", "tool_choice", "response_format", "stream", "reasoning", "extra_body"):
        if k in body:
            parts.append(f"{k}={type(body[k]).__name__}")
    return " ".join(parts)


def _relay_upstream_error(upstream: httpx.Response, request_id: str, body: object = None) -> JSONResponse:
    """Map upstream failures without leaking upstream auth details. Our own key
    being rejected must NOT surface as 401 (dsh would blame the user token)."""
    status = upstream.status_code
    if 400 <= status < 500 and status not in (401, 403, 429):
        # 上游说"你这个请求不对"时, 把**我们发出去的形状**也记下来 —— 否则这类
        # 报错在我们这边完全没有线索 (见 _body_shape)。
        try:
            why = upstream.json().get("error", {}).get("message", "")[:200]
        except (json.JSONDecodeError, AttributeError):
            why = upstream.text[:200]
        log.warning(
            "[gateway] 上游拒绝 %s rid=%s 上游说: %s | 我们发的形状: %s",
            status,
            request_id,
            why,
            _body_shape(body),
        )
    if status in (401, 403):
        return _openai_error(502, "upstream_error", "Upstream rejected the gateway. Contact support.")
    try:
        detail = upstream.json().get("error", {}).get("message", "")[:300]
    except (json.JSONDecodeError, AttributeError):
        detail = ""
    if status == 429:
        return _openai_error(429, "rate_limit_exceeded", detail or "Upstream is rate limiting.")
    if status >= 500:
        return _openai_error(502, "upstream_error", detail or "Upstream server error.")
    return _openai_error(status, "invalid_request_error", detail or "Upstream rejected the request.")


def _sse_error_bytes(status: int, detail: bytes) -> bytes:
    try:
        message = json.loads(detail).get("error", {}).get("message", "")[:300]
    except (json.JSONDecodeError, AttributeError):
        message = ""
    payload = {
        "error": {
            "message": message or f"Upstream error {status}",
            "type": "upstream_error",
            "code": "upstream_error",
        }
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n"


# --- Anthropic Messages ------------------------------------------------------


def _is_web_search(body: object) -> bool:
    """这一发是不是 dsh 的 web_search。

    以前这个接口**无条件**当搜索处理 (只看 SEARCH_PROVIDER), 因为它当初就是为
    web_search 建的。于是任何 Anthropic 客户端 (Claude Code 这类) 指过来跑正常
    对话, 每一发都只会拿到一份搜索结果 —— 而且不报错, 看着像模型犯傻。
    2026-08-31 评估接入 Claude Code 时发现。

    dsh 发搜索有**两种**形状, 两种都要认 (漏一种就是把真搜索当对话转发出去,
    上游没有 web_search 工具, 用户那边表现为搜索功能整个失灵):
      1. tools 里带一个 name=web_search 的服务端工具;
      2. 用户消息以 "Perform a web search for the query:" 开头 (zhipu_search
         的 _QUERY_PREFIX 认的就是它)。
    """
    if not isinstance(body, dict):
        return False
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if t.get("name") == "web_search" or str(t.get("type", "")).startswith("web_search"):
            return True
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        text = (
            c
            if isinstance(c, str)
            else " ".join(
                b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text"
            )
        )
        if zhipu_search._QUERY_PREFIX.match(text or ""):
            return True
    return False


@router.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request, user: dict = Depends(resolve_user)):
    rejected = _admit(user)
    if rejected is not None:
        return rejected

    raw = await read_limited_body(
        request,
        max_bytes=config.GATEWAY_BODY_MAX_BYTES,
        timeout_s=config.REQUEST_BODY_TIMEOUT_S,
    )
    request_id = f"dhc-{uuid.uuid4().hex[:16]}"

    # Zhipu-backed web_search: translate the Anthropic request to a Zhipu
    # search call and synthesize the native result blocks dsh expects. Avoids
    # DeepSeek's paid search endpoint entirely.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if config.SEARCH_PROVIDER == "zhipu" and _is_web_search(parsed):
        if not config.ZHIPU_SEARCH_API_KEY:
            raise HTTPException(503, "search_not_configured")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "Body must be JSON."},
                },
            )
        model = str(body.get("model", "")) or model_catalog.default_model()
        query = zhipu_search.extract_query(body)
        with _Slot(user["id"]):
            try:
                results = await zhipu_search.search(query, zhipu_search._max_results(body))
            except (httpx.HTTPError, ValueError):
                results = []
        # Only searches that return usable results are billable; empty results
        # may be retried by the client.
        if results:
            credits.spend(
                user["id"],
                config.SEARCH_CALL_CREDITS,
                kind="search",
                model="web_search:zhipu",
                device_id=user.get("device_id", ""),
                request_id=request_id,
            )
        return JSONResponse(
            content=zhipu_search.to_anthropic_response(query, results, model),
            headers={"x-request-id": request_id},
        )

    _require_upstream()
    headers = {
        "x-api-key": config.UPSTREAM_API_KEY,
        "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
        "content-type": "application/json",
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
    }
    url = config.UPSTREAM_ANTHROPIC_BASE.rstrip("/") + "/messages"

    def bill(
        usage: dict | None,
        forwarded_bytes: int = 0,
        *,
        interrupted: bool = False,
    ) -> None:
        u = dict(usage or {})
        if u.get("input_tokens") is None:
            u["input_tokens"] = max(1, math.ceil(len(raw) / STREAM_FALLBACK_BYTES_PER_TOKEN))
        known_output = int(u.get("output_tokens") or 0)
        if u.get("output_tokens") is None or interrupted:
            fallback_output = max(1, math.ceil(forwarded_bytes / STREAM_FALLBACK_BYTES_PER_TOKEN))
            known_output = max(known_output, fallback_output)
        inp = int(u.get("input_tokens") or 0)
        out = known_output
        # 这条路是**普通对话**的转发 (搜索走上面那个分支, 自己结自己的账)。
        # 早先这里写死成 kind="search" + 搜索固定费 + 默认模型的价 —— 那是整个
        # 接口只服务 web_search 时代的遗留。真跑对话的话, 用户会被按默认模型
        # 计价再加一笔搜索费, 而他用的可能是贵十倍的 claude-opus-5。
        billed_model = str((parsed or {}).get("model") or "") or model_catalog.default_model()
        amount = 0
        if inp or out:
            amount = model_catalog.charge_credits(billed_model, inp, 0, out)
        credits.spend(
            user["id"],
            amount,
            kind="llm",
            model=billed_model,
            device_id=user.get("device_id", ""),
            uncached_input=inp,
            output=out,
            request_id=request_id,
        )

    stream_requested = False
    try:
        stream_requested = bool(json.loads(raw).get("stream", False))
    except json.JSONDecodeError:
        pass

    if not stream_requested:
        async with _upstream_client() as client:
            with _Slot(user["id"]):
                upstream = await client.post(url, content=raw, headers=headers)
            if upstream.status_code == 200:
                data = upstream.json()
                bill(data.get("usage"))
                return JSONResponse(content=data, headers={"x-request-id": request_id})
            body_snip = upstream.text[:300]
            status = (
                502
                if upstream.status_code in (401, 403) or upstream.status_code >= 500
                else upstream.status_code
            )
            return JSONResponse(
                status_code=status,
                content={"type": "error", "error": {"type": "api_error", "message": body_snip}},
            )

    async def relay():
        slot = _Slot(user["id"])
        slot.__enter__()
        usage: dict = {}
        forwarded_bytes = 0
        upstream_started = False
        stream_exhausted = False
        stream_complete = False
        buffer = b""
        try:
            async with _upstream_client() as client:
                async with client.stream("POST", url, content=raw, headers=headers) as upstream:
                    if not 200 <= upstream.status_code < 300:
                        detail = await upstream.aread()
                        yield _sse_error_bytes(upstream.status_code, detail)
                        return
                    upstream_started = True
                    async for chunk in upstream.aiter_raw():
                        forwarded_bytes += len(chunk)
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            text = line.strip()
                            if text == b"event: message_stop":
                                stream_complete = True
                            if text.startswith(b"data:"):
                                try:
                                    parsed = json.loads(text[5:].strip())
                                    if parsed.get("type") == "message_stop":
                                        stream_complete = True
                                    event_usage = (
                                        parsed.get("usage")
                                        or (parsed.get("message") or {}).get("usage")
                                        or {}
                                    )
                                    if isinstance(event_usage, dict):
                                        usage.update(event_usage)
                                except (json.JSONDecodeError, AttributeError):
                                    pass
                        yield chunk
                    stream_exhausted = True
        finally:
            slot.__exit__()
            if upstream_started and (forwarded_bytes or not stream_exhausted):
                try:
                    bill(usage or None, forwarded_bytes, interrupted=not stream_complete)
                except Exception:
                    log.exception("failed to bill Anthropic stream request_id=%s", request_id)

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"x-request-id": request_id})


# --- catalog ----------------------------------------------------------------


@router.get("/v1/models")
def list_models(user: dict = Depends(resolve_user)):
    data = [
        {
            "id": m["id"],
            "object": "model",
            "owned_by": "dsh-cloud",
            "display_name": m.get("display_name", m["id"]),
            "context_window": m.get("context_window"),
        }
        for m in model_catalog.catalog().values()
    ]
    return {"object": "list", "data": data}
