"""Password hashing and HMAC session tokens.

No third-party JWT dependency. Token format: base64url(json).hmac_sha256_hex
Payload: {"u": user_id, "d": device_id|"", "e": epoch, "exp": unix_ts}

Revocation gate: users.session_epoch is stored per user; a token is only valid
while its "e" matches. Password change / account deletion / kick-devices bumps
the epoch and every outstanding token dies at once.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from . import config


def _secret() -> bytes:
    s = config.auth_secret()
    if not s and not config.DEV_MODE:
        raise RuntimeError("AUTH_SECRET is not set")
    return (s or "dev-secret-do-not-use").encode()


# --- passwords --------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# --- tokens -----------------------------------------------------------------

def sign_token(user_id: str, device_id: str = "", epoch: int = 0, ttl: int | None = None) -> str:
    payload = {"u": user_id, "d": device_id, "e": int(epoch),
               "exp": time.time() + (ttl if ttl is not None else config.SESSION_TTL)}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_token(token: str) -> dict | None:
    """Returns the payload dict, or None. Caller still checks epoch + user status."""
    try:
        raw, sig = token.rsplit(".", 1)
        expect = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if float(payload.get("exp", 0)) < time.time():
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def token_hash(token: str) -> str:
    """Storage form for long-lived device tokens (plaintext never persisted)."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(12)


def user_code() -> str:
    """Human-typable device activation code, e.g. 7GK4-XQ2M (no 0/O/1/I)."""
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    chars = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{chars[:4]}-{chars[4:]}"
