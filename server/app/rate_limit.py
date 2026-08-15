"""In-process sliding-window rate limits and login brute-force lockout.

Single-worker semantics. For multi-worker deployments put a shared Redis in
front (same approach as a sibling production system) — the interfaces here are the seam.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_windows: dict[str, deque] = defaultdict(deque)


def allow(key: str, limit: int, window_s: float) -> bool:
    """True if the event is allowed; records it when allowed."""
    now = time.time()
    with _lock:
        q = _windows[key]
        while q and q[0] <= now - window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def login_failed(account: str, ip: str) -> None:
    now = time.time()
    with _lock:
        _windows[f"lf:a:{account}"].append(now)
        _windows[f"lf:i:{ip}"].append(now)


def login_locked(account: str, ip: str) -> bool:
    """5 failures / 15 min per account, 30 / 15 min per IP. Check BEFORE verifying."""
    now = time.time()
    with _lock:
        for key, limit in ((f"lf:a:{account}", 5), (f"lf:i:{ip}", 30)):
            q = _windows[key]
            while q and q[0] <= now - 900:
                q.popleft()
            if len(q) >= limit:
                return True
    return False


class TokenBucket:
    """Per-key QPS bucket for the gateway."""

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            tokens, last = self._state.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self._state[key] = (tokens, now)
                return False
            self._state[key] = (tokens - 1.0, now)
            return True
