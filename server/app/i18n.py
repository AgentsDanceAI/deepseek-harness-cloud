"""Bilingual copy for the site.

Keys live in config/i18n/{lang}.json and are looked up through `t()`, which is
exposed to every template. Two design choices worth stating:

* **Keys, not English source strings.** A key like `nav.pricing` survives a copy
  edit in either language; using the Chinese text as the key means every wording
  tweak silently orphans its translation.
* **Fall back to the default language, then to the key itself.** A missing
  translation should degrade to the other language's real sentence, never to a
  blank — a page with holes in it is worse than a page that is briefly mixed.
  Rendering the raw key is the last resort and is deliberately ugly so it gets
  noticed in review rather than shipping unseen.

Language resolution order: explicit ?lang= (and it sticks, via cookie) ->
cookie -> Accept-Language -> DEFAULT_LANG. The explicit choice wins over the
browser because someone who clicked "EN" means it, even on a zh-CN laptop.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import config

SUPPORTED = ("zh", "en")
DEFAULT = "zh"
COOKIE = "dhc_lang"
COOKIE_MAX_AGE = 365 * 24 * 3600

_lock = threading.Lock()
_cache: dict[str, dict] = {}
_mtimes: dict[str, float] = {}


def _path(lang: str) -> Path:
    return config.CONFIG_DIR / "i18n" / f"{lang}.json"


def catalog(lang: str) -> dict:
    """Flat key -> string map for one language. Hot-reloads on mtime change."""
    lang = lang if lang in SUPPORTED else DEFAULT
    p = _path(lang)
    with _lock:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            _cache.setdefault(lang, {})
            return _cache[lang]
        if _mtimes.get(lang) != mtime:
            try:
                _cache[lang] = json.loads(p.read_text(encoding="utf-8"))
                _mtimes[lang] = mtime
            except (ValueError, OSError):
                _cache.setdefault(lang, {})
        return _cache[lang]


def t(lang: str, key: str, **fmt) -> str:
    """Translate `key`; `fmt` fills {placeholders} in the string."""
    value = catalog(lang).get(key)
    if value is None and lang != DEFAULT:
        value = catalog(DEFAULT).get(key)
    if value is None:
        return key
    if fmt:
        try:
            return value.format(**fmt)
        except (KeyError, IndexError, ValueError):
            # A malformed placeholder must not 500 a page; show the raw string.
            return value
    return value


def _from_accept_language(header: str) -> str | None:
    """First supported language in an Accept-Language header, by q-order."""
    entries = []
    for part in (header or "").split(","):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        q = 1.0
        if params.startswith("q="):
            try:
                q = float(params[2:])
            except ValueError:
                q = 0.0
        entries.append((q, tag.strip().lower()))
    for _q, tag in sorted(entries, key=lambda e: -e[0]):
        base = tag.split("-")[0]
        if base in SUPPORTED:
            return base
    return None


def resolve(request) -> tuple[str, bool]:
    """(language, explicit) for this request.

    `explicit` marks a ?lang= choice, which the caller persists as a cookie.
    """
    q = (request.query_params.get("lang") or "").strip().lower()
    if q in SUPPORTED:
        return q, True
    c = (request.cookies.get(COOKIE) or "").strip().lower()
    if c in SUPPORTED:
        return c, False
    return _from_accept_language(request.headers.get("accept-language", "")) or DEFAULT, False


def other(lang: str) -> str:
    return "en" if lang == "zh" else "zh"
