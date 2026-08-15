"""Model catalog and price math.

Listed prices are CNY per 1M tokens in config/models.json. 1 credit = ¥0.01 of
listed usage; the actual charge multiplies by MODEL_PRICE_MARKUP. All charges
round up to at least 1 credit so streaming freeloaders can't ride for free.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float = 0.0


def _path() -> Path:
    return config.CONFIG_DIR / "models.json"


def catalog() -> dict[str, dict]:
    """id -> model entry. Hot-reloads on file mtime change."""
    global _cache, _cache_mtime
    p = _path()
    mtime = p.stat().st_mtime
    with _lock:
        if _cache is None or mtime != _cache_mtime:
            data = json.loads(p.read_text())
            _cache = {m["id"]: m for m in data["models"]}
            _cache_mtime = mtime
        return _cache


def resolve(model_id: str) -> dict | None:
    return catalog().get(model_id)


def default_model() -> str:
    for m in catalog().values():
        if m.get("default"):
            return m["id"]
    return next(iter(catalog()))


def charge_credits(model_id: str, uncached_input: int, cache_read: int, output: int) -> int:
    """Credits to charge for one completed request. >= 1 whenever any tokens flowed."""
    m = resolve(model_id)
    if m is None:
        # Unknown model that somehow got through: bill at the priciest entry.
        m = max(catalog().values(), key=lambda x: x["output_cny_per_m"])
    cny = (
        uncached_input * m["input_cny_per_m"]
        + cache_read * m.get("cache_read_cny_per_m", m["input_cny_per_m"])
        + output * m["output_cny_per_m"]
    ) / 1_000_000
    credits = math.ceil(cny * 100 * config.MODEL_PRICE_MARKUP)
    if credits < 1 and (uncached_input or cache_read or output):
        credits = 1
    return credits
