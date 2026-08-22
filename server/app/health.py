"""Liveness, dependency readiness, and non-secret release metadata."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import config, db, plans

router = APIRouter(tags=["health"])


def liveness() -> dict[str, str]:
    """Process-only signal; deliberately makes no dependency claims."""
    return {"status": "alive"}


@router.get("/livez")
def livez():
    return liveness()


@router.get("/readyz")
def readyz():
    checks: dict[str, str] = {}
    try:
        db.query_one("SELECT 1 AS ok")
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness converts dependency failures
        checks["database"] = f"error:{type(exc).__name__}"

    try:
        pricing = plans.pricing()
        if not isinstance(pricing.get("tiers"), dict) or not pricing["tiers"]:
            raise ValueError("missing tiers")
        checks["pricing"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness converts dependency failures
        checks["pricing"] = f"error:{type(exc).__name__}"

    if config.WAFFO_MERCHANT_ID and config.WAFFO_PRIVATE_KEY:
        checks["waffo_webhook_key"] = "ok" if config.WAFFO_WEBHOOK_PUBLIC_KEY else "error:missing"

    ready = all(value == "ok" for value in checks.values())
    payload = {"status": "ready" if ready else "not_ready", "checks": checks}
    if ready:
        return payload
    return JSONResponse(status_code=503, content=payload)


@router.get("/version")
def version():
    return {"version": config.RELEASE_VERSION, "revision": config.RELEASE_REVISION}


@router.get("/api/health")
def legacy_health():
    """Compatibility alias with its historical payload and liveness semantics."""
    return {"ok": True, "service": "deepseek-harness-cloud"}
