"""Model catalog and price math.

config/models.json is GENERATED (server/scripts/gen_models.py) from the gateway's
catalog plus a price table — never hand-edited, because a hand-kept price table
drifts and nobody notices.

Credit convention used by every catalog and entitlement path:
  * blended price  = input * 0.75 + output * 0.25   (USD per 1M tokens)
  * multiplier     = blended / blended(baseline)     ← Claude Sonnet is 1.00x
  * 1.00x          = 1000 credits per 1M tokens
  * $1             = 100 credits
The multiplier is a ratio, so changing MODEL_PRICE_MARKUP moves the charge but
never the advertised multiplier.

Every charge rounds up to at least 1 credit, so streaming freeloaders can't ride
for free on sub-credit requests.

Embedding models live in a second section of the same file and price on INPUT
tokens alone — they have no output and no prompt cache. They are kept out of
`catalog()` on purpose: that one is "models you can chat with", and it is what
`GET /v1/models` hands to dsh.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()
_cache: dict | None = None
_embeddings: dict = {}
_meta: dict = {}
_cache_mtime: float = 0.0

CREDITS_PER_USD = 100  # $1 = 100 credits


def _path() -> Path:
    return config.CONFIG_DIR / "models.json"


def _load() -> None:
    global _cache, _embeddings, _meta, _cache_mtime
    p = _path()
    mtime = p.stat().st_mtime
    if _cache is None or mtime != _cache_mtime:
        data = json.loads(p.read_text())
        _cache = {m["id"]: m for m in data["models"]}
        _embeddings = {m["id"]: m for m in data.get("embedding_models", [])}
        _meta = {k: v for k, v in data.items() if k not in ("models", "embedding_models")}
        _cache_mtime = mtime


def catalog() -> dict[str, dict]:
    """id -> model entry. Hot-reloads on file mtime change."""
    with _lock:
        _load()
        return _cache


def meta() -> dict:
    with _lock:
        _load()
        return _meta


def resolve(model_id: str) -> dict | None:
    return catalog().get(model_id)


def default_model() -> str:
    for m in catalog().values():
        if m.get("default"):
            return m["id"]
    return next(iter(catalog()))


def embedding_catalog() -> dict[str, dict]:
    """id -> embedding entry. Deliberately a SEPARATE catalog from the chat one.

    Merging them would put a vector model in `GET /v1/models`, and dsh renders
    that list as "models you can chat with" — picking one there fails upstream
    with an error that says nothing about why.
    """
    with _lock:
        _load()
        return _embeddings


def resolve_embedding(model_id: str) -> dict | None:
    return embedding_catalog().get(model_id)


def default_embedding_model() -> str:
    for m in embedding_catalog().values():
        if m.get("default"):
            return m["id"]
    return next(iter(embedding_catalog()), "")


def _usd_per_m(entry: dict, field: str) -> float:
    """Price in USD per 1M tokens, tolerating the pre-generator CNY schema."""
    if f"{field}_usd_per_m" in entry:
        return float(entry[f"{field}_usd_per_m"])
    cny = entry.get(f"{field}_cny_per_m")
    if cny is not None:
        return float(cny) / 7.2  # frozen display rate; only legacy files hit this
    return 0.0


def charge_credits(model_id: str, uncached_input: int, cache_read: int, output: int) -> int:
    """Credits to charge for one completed request. >= 1 whenever any tokens flowed."""
    m = resolve(model_id)
    if m is None:
        # Unknown model that somehow got through: bill at the priciest entry so a
        # gap in the catalog can never become a free ride.
        m = max(catalog().values(), key=lambda x: _usd_per_m(x, "output"))
    input_usd = _usd_per_m(m, "input")
    output_usd = _usd_per_m(m, "output")
    # Cached prompt tokens are far cheaper upstream; when the catalog does not
    # say, assume the common 10% of the input rate rather than full price.
    cache_usd = _usd_per_m(m, "cache_read") or input_usd * 0.1
    usd = (uncached_input * input_usd + cache_read * cache_usd + output * output_usd) / 1_000_000
    credits = math.ceil(usd * CREDITS_PER_USD * config.MODEL_PRICE_MARKUP)
    if credits < 1 and (uncached_input or cache_read or output):
        credits = 1
    return credits


def charge_embedding_credits(model_id: str, input_tokens: int) -> int:
    """Credits for one completed embeddings request. Input tokens only.

    An embedding model has no output tokens and no prompt cache, so the chat
    formula's other two terms are meaningless for it. Worse, `charge_credits`
    resolves against the CHAT catalog, where these ids do not exist — it would
    fall through to its "priciest entry" guard and bill a knowledge-base import
    at the most expensive chat model's output rate. That is the failure this
    function exists to prevent, so it must stay the only path for /v1/embeddings.
    """
    m = resolve_embedding(model_id)
    if m is None:
        # Same principle as chat: an id that slipped past the catalog check is
        # billed at the priciest entry rather than free. Fall back to the chat
        # catalog only if there are no embedding models at all.
        pool = embedding_catalog() or catalog()
        m = max(pool.values(), key=lambda x: _usd_per_m(x, "input"))
    usd = input_tokens * _usd_per_m(m, "input") / 1_000_000
    credits = math.ceil(usd * CREDITS_PER_USD * config.MODEL_PRICE_MARKUP)
    # Upstream gives BGE-M3 away, and a lot of short chunks round to nothing —
    # a request that did work still costs us a slot, so it costs at least one.
    if credits < 1 and input_tokens:
        credits = 1
    return credits


_CAPS_CACHE: dict | None = None
_CAPS_MTIME: float = 0.0


def capabilities(model_id: str) -> dict:
    """型号的能力元数据: reasoning / vision / context_window。

    价目表 (models.json) 是生成的, 不放这些; 能力是上游事实, 手工维护在
    config/model_capabilities.json 里, 按 id 覆盖, 缺的用 default。给需要
    "型号清单 + 能力"的产品用 (pi 的 models.json 就是照这个生成的) —— 之前这类
    信息散在各产品的启动脚本里, 换个型号要改好几处。
    """
    global _CAPS_CACHE, _CAPS_MTIME
    p = config.CONFIG_DIR / "model_capabilities.json"
    with _lock:
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            return {"reasoning": True, "vision": False, "context_window": 128000}
        if _CAPS_CACHE is None or mtime != _CAPS_MTIME:
            _CAPS_CACHE = json.loads(p.read_text())
            _CAPS_MTIME = mtime
        out = dict(_CAPS_CACHE.get("default") or {})
        out.update((_CAPS_CACHE.get("models") or {}).get(model_id) or {})
        return out


def public_catalog() -> list[dict]:
    """Catalog for the pricing page: what a model costs, in credits."""
    out = []
    for m in catalog().values():
        out.append(
            {
                "id": m["id"],
                "name": m.get("display_name", m["id"]),
                "provider": m.get("provider", ""),
                "multiplier": m.get("multiplier"),
                "credits_per_m": m.get("credits_per_m"),
                "default": bool(m.get("default")),
            }
        )
    out.sort(key=lambda m: (m["multiplier"] is None, m["multiplier"] or 0, m["id"]))
    return out
