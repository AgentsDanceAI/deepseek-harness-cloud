"""Canonical validation for browser destinations that must stay on this site."""

from __future__ import annotations

from urllib.parse import unquote


def safe_local_path(value: str, fallback: str = "/console") -> str:
    """Return ``value`` only when browser normalization cannot make it external."""
    candidate = (value or "").strip()
    try:
        decoded = unquote(candidate)
    except (UnicodeDecodeError, ValueError):
        return fallback
    if not decoded.startswith("/") or decoded.startswith("//"):
        return fallback
    if "\\" in decoded or "\x00" in decoded or any(ord(ch) < 32 for ch in decoded):
        return fallback
    return candidate
