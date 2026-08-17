"""GitHub star count for the header badge.

The badge exists to show traction once the repository is public. Until then the
repo answers 404 to anonymous callers, so the badge renders nothing at all
rather than a zero — a "⭐ 0" next to a link that 404s is worse than no badge.
It starts appearing on its own the moment the repo goes public; no redeploy.

Three things this has to get right:

* Fetch on the SERVER. A browser-side call would spend each visitor's own IP
  against GitHub's 60-requests-per-hour unauthenticated limit, and the badge
  would break for exactly the visitors who arrive in a burst.
* Never flicker. GitHub rate-limiting us or going down must not blank a badge
  that was there a second ago, so the last successful value is kept and served
  past its refresh deadline.
* Survive restarts. The value is persisted, so a redeploy does not blank the
  badge until the first refresh completes.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from . import config, db

log = logging.getLogger("dhc.stars")

REPO = "AgentsDanceAI/deepseek-harness-cloud"
KV_KEY = "github_stars"
REFRESH_AFTER = 1800     # seconds before a value is considered stale
RETRY_AFTER = 300        # do not hammer GitHub when it is failing

_lock = threading.Lock()
_refreshing = False
_last_attempt = 0.0


def _read() -> dict:
    row = db.query_one("SELECT v FROM kv WHERE k=?", (KV_KEY,))
    if row is None:
        return {}
    try:
        return json.loads(row["v"])
    except (ValueError, TypeError):
        return {}


def _write(state: dict) -> None:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?, ?) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
            (KV_KEY, json.dumps(state)))


def _fetch() -> None:
    """Refresh in the background; failures leave the previous value alone."""
    global _refreshing
    try:
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "deepseek-harness-cloud"}
        r = httpx.get(f"https://api.github.com/repos/{REPO}",
                      headers=headers, timeout=8.0)
        if r.status_code == 200:
            stars = int(r.json().get("stargazers_count", 0))
            _write({"stars": stars, "public": True, "checked": time.time()})
            log.info("github stars: %s", stars)
        elif r.status_code == 404:
            # Private or renamed. Anonymous visitors see the same 404, so the
            # badge must stay hidden — this is the expected state pre-launch.
            _write({"stars": None, "public": False, "checked": time.time()})
        else:
            log.warning("github stars: unexpected %s", r.status_code)
    except Exception as exc:  # noqa: BLE001 — a badge must never break a page
        log.warning("github stars: %s", exc)
    finally:
        with _lock:
            _refreshing = False


def stars() -> int | None:
    """Star count, or None when the repo is not publicly visible.

    Never blocks on the network: a stale value is served while a refresh runs.
    """
    global _refreshing, _last_attempt
    state = _read()
    age = time.time() - float(state.get("checked") or 0)
    if age > REFRESH_AFTER:
        now = time.time()
        with _lock:
            if not _refreshing and now - _last_attempt > RETRY_AFTER:
                _refreshing = True
                _last_attempt = now
                threading.Thread(target=_fetch, daemon=True).start()
    return state.get("stars") if state.get("public") else None


def format_count(n: int) -> str:
    """11123 -> '11.1K'. Matches how GitHub itself renders the number."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def repo_url() -> str:
    return f"https://github.com/{REPO}"
