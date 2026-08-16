"""The LLM gateway. The upstream API key lives ONLY here, server-side.

Two surfaces, matching exactly what dsh emits:

  POST /llm/v1/chat/completions   OpenAI-compatible chat completions (llm-deepseek
                                  adapter; always stream:true + include_usage)
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

import asyncio
import json
import threading
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, credits, model_catalog, plans, rate_limit, zhipu_search
from .accounts import resolve_user

router = APIRouter(prefix="/llm", tags=["gateway"])

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
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": code, "code": code}})


def _admit(user: dict) -> JSONResponse | None:
    """Returns an error response if the request must be rejected, else None."""
    uid = user["id"]
    limit = plans.concurrency_limit(uid)
    if _inflight.get(uid, 0) >= limit:
        return _openai_error(429, "concurrency_limit",
                             f"Plan allows {limit} concurrent request(s). Upgrade for more.")
    if not _qps.take(uid):
        return _openai_error(429, "rate_limit_exceeded", "Too many requests, slow down.")
    reason = plans.check_run_blocked(uid)
    if reason:
        return _openai_error(402, "insufficient_quota",
                             "Credit balance exhausted. Top up or upgrade at "
                             f"{config.PUBLIC_BASE}/pricing to continue.")
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
        body = json.loads(await request.body())
    except json.JSONDecodeError:
        return _openai_error(400, "invalid_request_error", "Body must be JSON.")

    model_id = str(body.get("model", "")) or model_catalog.default_model()
    entry = model_catalog.resolve(model_id)
    if entry is None:
        return _openai_error(404, "model_not_found",
                             f"Model '{model_id}' is not offered. See GET /llm/v1/models.")
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

    def bill(usage: dict | None) -> None:
        u = usage or {}
        cache_read = int(u.get("prompt_cache_hit_tokens") or
                         (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        prompt = int(u.get("prompt_tokens") or 0)
        uncached = max(0, prompt - cache_read)
        output = int(u.get("completion_tokens") or 0)
        amount = model_catalog.charge_credits(model_id, uncached, cache_read, output)
        if not usage:
            amount = max(amount, 1)  # stream died before the usage chunk: floor charge
        credits.spend(user["id"], amount, kind="llm", model=model_id,
                      device_id=user.get("device_id", ""), uncached_input=uncached,
                      cache_read=cache_read, output=output, request_id=request_id)

    if not stream:
        async with _upstream_client() as client:
            with _Slot(user["id"]):
                upstream = await client.post(url, json=body, headers=headers)
            if upstream.status_code == 200:
                data = upstream.json()
                bill(data.get("usage"))
                return JSONResponse(content=data, headers={"x-request-id": request_id})
            return _relay_upstream_error(upstream, request_id)

    async def relay():
        slot = _Slot(user["id"])
        slot.__enter__()
        usage: dict | None = None
        buffer = b""
        try:
            async with _upstream_client() as client:
                async with client.stream("POST", url, json=body, headers=headers) as upstream:
                    if upstream.status_code != 200:
                        detail = await upstream.aread()
                        yield _sse_error_bytes(upstream.status_code, detail)
                        return
                    async for chunk in upstream.aiter_raw():
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
        finally:
            slot.__exit__()
            try:
                bill(usage)
            except Exception:
                pass  # billing must never break the response

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"x-request-id": request_id,
                                      "cache-control": "no-cache"})


def _relay_upstream_error(upstream: httpx.Response, request_id: str) -> JSONResponse:
    """Map upstream failures without leaking upstream auth details. Our own key
    being rejected must NOT surface as 401 (dsh would blame the user token)."""
    status = upstream.status_code
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
    payload = {"error": {"message": message or f"Upstream error {status}",
                         "type": "upstream_error", "code": "upstream_error"}}
    return b"data: " + json.dumps(payload).encode() + b"\n\ndata: [DONE]\n\n"


# --- Anthropic Messages passthrough (dsh web_search) ------------------------

@router.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request, user: dict = Depends(resolve_user)):
    rejected = _admit(user)
    if rejected is not None:
        return rejected

    raw = await request.body()
    request_id = f"dhc-{uuid.uuid4().hex[:16]}"

    # Zhipu-backed web_search: translate the Anthropic request to a Zhipu
    # search call and synthesize the native result blocks dsh expects. Avoids
    # DeepSeek's paid search endpoint entirely.
    if config.SEARCH_PROVIDER == "zhipu":
        if not config.ZHIPU_SEARCH_API_KEY:
            raise HTTPException(503, "search_not_configured")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={
                "type": "error", "error": {"type": "invalid_request_error", "message": "Body must be JSON."}})
        model = str(body.get("model", "")) or model_catalog.default_model()
        query = zhipu_search.extract_query(body)
        with _Slot(user["id"]):
            try:
                results = await zhipu_search.search(query, zhipu_search._max_results(body))
            except (httpx.HTTPError, ValueError):
                results = []
        credits.spend(user["id"], config.SEARCH_CALL_CREDITS, kind="search", model="web_search:zhipu",
                      device_id=user.get("device_id", ""), request_id=request_id)
        return JSONResponse(content=zhipu_search.to_anthropic_response(query, results, model),
                            headers={"x-request-id": request_id})

    _require_upstream()
    headers = {
        "x-api-key": config.UPSTREAM_API_KEY,
        "authorization": f"Bearer {config.UPSTREAM_API_KEY}",
        "content-type": "application/json",
        "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
    }
    url = config.UPSTREAM_ANTHROPIC_BASE.rstrip("/") + "/messages"

    def bill(usage: dict | None) -> None:
        u = usage or {}
        inp = int(u.get("input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        amount = config.SEARCH_CALL_CREDITS
        if inp or out:
            amount += model_catalog.charge_credits(model_catalog.default_model(), inp, 0, out)
        credits.spend(user["id"], amount, kind="search", model="web_search",
                      device_id=user.get("device_id", ""), uncached_input=inp,
                      output=out, request_id=request_id)

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
            status = 502 if upstream.status_code in (401, 403) or upstream.status_code >= 500 \
                else upstream.status_code
            return JSONResponse(status_code=status, content={
                "type": "error", "error": {"type": "api_error", "message": body_snip}})

    async def relay():
        slot = _Slot(user["id"])
        slot.__enter__()
        usage: dict = {}
        buffer = b""
        try:
            async with _upstream_client() as client:
                async with client.stream("POST", url, content=raw, headers=headers) as upstream:
                    async for chunk in upstream.aiter_raw():
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            text = line.strip()
                            if text.startswith(b"data:") and b'"usage"' in text:
                                try:
                                    parsed = json.loads(text[5:].strip())
                                    usage.update(parsed.get("usage") or
                                                 (parsed.get("message") or {}).get("usage") or {})
                                except (json.JSONDecodeError, AttributeError):
                                    pass
                        yield chunk
        finally:
            slot.__exit__()
            try:
                bill(usage or None)
            except Exception:
                pass

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"x-request-id": request_id})


# --- catalog ----------------------------------------------------------------

@router.get("/v1/models")
def list_models(user: dict = Depends(resolve_user)):
    data = [{"id": m["id"], "object": "model", "owned_by": "dsh-cloud",
             "display_name": m.get("display_name", m["id"]),
             "context_window": m.get("context_window")}
            for m in model_catalog.catalog().values()]
    return {"object": "list", "data": data}
