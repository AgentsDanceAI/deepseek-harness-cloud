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

    # The workspace runs on its own subdomain and its injected chrome asks this
    # origin for quota/sign-out. Same site (so the session cookie is sent), but
    # a different origin, so it needs an explicit CORS grant — scoped to exactly
    # that host, never a wildcard, because these endpoints act with the session.
    if config.WORK_DOMAIN:
        from fastapi.middleware.cors import CORSMiddleware
        scheme = "http" if config.PUBLIC_BASE.startswith("http://") else "https"
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[f"{scheme}://{config.WORK_DOMAIN}"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["content-type"],
        )

    from .accounts import router as accounts_router
    from .admin import router as admin_router
    from .desktop_updates import router as updates_router
    from .device_auth import router as device_router
    from .gateway import router as gateway_router
    from .oauth import router as oauth_router
    from .payments.api import router as payments_router
    from .webpages import router as pages_router
    from .teams import router as teams_router
    from .workspace import preview_fallback as workspace_preview_fallback
    from .workspace import router as workspace_router

    app.include_router(accounts_router)
    app.include_router(oauth_router)
    app.include_router(device_router)
    app.include_router(gateway_router)
    app.include_router(payments_router)
    app.include_router(admin_router)
    app.include_router(updates_router)
    app.include_router(workspace_router)
    app.include_router(teams_router)

    if config.WORK_ENABLED:
        from .workspace import billing_reaper_loop

        @app.on_event("startup")
        async def _start_workspace_loop() -> None:
            import asyncio
            app.state.workspace_loop = asyncio.create_task(billing_reaper_loop())

    # A wrong WORK_VOLUME_ROOT does not fail anything — it just makes 個人成品
    # show nothing for every user with a stopped workspace. That is exactly the
    # kind of breakage a machine move introduces (the host's docker root is
    # /mnt/docker here, /var/lib/docker on a default install), so say it once
    # at boot rather than let it be discovered by a confused user.
    if config.WORK_VOLUME_ROOT and not Path(config.WORK_VOLUME_ROOT).is_dir():
        log.warning("WORK_VOLUME_ROOT=%s is not a directory in this container — "
                    "个人成品 will be empty whenever a workspace is stopped. "
                    "Check DOCKER_VOLUME_ROOT in .env against "
                    "`docker info -f '{{.DockerRootDir}}'`/volumes on the host.",
                    config.WORK_VOLUME_ROOT)

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Desktop installers, served from the data volume (drop files into
    # $DHC_DATA_DIR/releases and point DOWNLOAD_URL_* at /releases/<file>).
    releases_dir = config.DATA_DIR / "releases"
    try:
        releases_dir.mkdir(parents=True, exist_ok=True)
        app.mount("/releases", StaticFiles(directory=str(releases_dir)), name="releases")
        # Whatever hosts the bytes long-term, this path stays as the fallback
        # (and is the only path a self-hoster has). Bound it so a few large
        # transfers cannot starve the gateway and the workspaces beside it.
        from .release_throttle import ReleaseThrottle
        app.add_middleware(ReleaseThrottle)  # raw ASGI: holds the slot until the body ends
    except OSError:
        log.warning("releases dir unavailable at %s", releases_dir)
    app.include_router(pages_router)  # last: contains catch-all-ish page routes

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "deepseek-harness-cloud"}

    # Registered after every real route, so it only ever sees requests that
    # would otherwise 404: a previewed page asking for an absolute-path asset
    # ("/style.css") from outside its /preview/<port>/ prefix. The preview
    # cookie says which port that page came from.
    app.add_route("/{path:path}", workspace_preview_fallback,
                  methods=["GET", "HEAD"])

    return app


app = create_app()
