"""FastAPI application assembly."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from . import config, db

log = logging.getLogger("dhc")


def create_app() -> FastAPI:
    if not config.auth_secret() and not config.DEV_MODE:
        raise RuntimeError("AUTH_SECRET must be set (or DHC_DEV=1 for local development)")
    if not config.UPSTREAM_API_KEY:
        log.warning("UPSTREAM_API_KEY is not set — the LLM gateway will answer 503")

    app = FastAPI(title="deepseek-harness-cloud", docs_url="/api/docs" if config.DEV_MODE else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if config.DEV_MODE else None)

    db.ensure_schema()

    from .accounts import router as accounts_router
    from .admin import router as admin_router
    from .desktop_updates import router as updates_router
    from .device_auth import router as device_router
    from .gateway import router as gateway_router
    from .oauth import router as oauth_router
    from .payments.api import router as payments_router
    from .webpages import router as pages_router
    from .workspace import router as workspace_router

    app.include_router(accounts_router)
    app.include_router(oauth_router)
    app.include_router(device_router)
    app.include_router(gateway_router)
    app.include_router(payments_router)
    app.include_router(admin_router)
    app.include_router(updates_router)
    app.include_router(workspace_router)

    if config.WORK_ENABLED:
        from .workspace import billing_reaper_loop

        @app.on_event("startup")
        async def _start_workspace_loop() -> None:
            import asyncio
            app.state.workspace_loop = asyncio.create_task(billing_reaper_loop())

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Desktop installers, served from the data volume (drop files into
    # $DHC_DATA_DIR/releases and point DOWNLOAD_URL_* at /releases/<file>).
    releases_dir = config.DATA_DIR / "releases"
    try:
        releases_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/releases", StaticFiles(directory=str(releases_dir)), name="releases")
    except OSError:
        log.warning("releases dir unavailable at %s", releases_dir)
    app.include_router(pages_router)  # last: contains catch-all-ish page routes

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "deepseek-harness-cloud"}

    return app


app = create_app()
