"""FastAPI application assembly."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db

log = logging.getLogger("dhc")


def setup_logging() -> None:
    """Configure application logs without changing uvicorn's loggers."""
    level = (config.LOG_LEVEL or "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger("dhc")
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False


def validate_startup_config() -> None:
    if not config.auth_secret() and not config.DEV_MODE:
        raise RuntimeError("AUTH_SECRET must be set (or DHC_DEV=1 for local development)")
    # Preserve the historical enablement boundary: a lone stale variable does
    # not activate Waffo or block unrelated payment providers. Once checkout
    # credentials are complete, signed webhooks are mandatory.
    waffo_enabled = bool(config.WAFFO_MERCHANT_ID and config.WAFFO_PRIVATE_KEY)
    if waffo_enabled and not config.WAFFO_WEBHOOK_PUBLIC_KEY:
        raise RuntimeError("WAFFO_WEBHOOK_PUBLIC_KEY is required whenever Waffo checkout is enabled")


def create_app() -> FastAPI:
    setup_logging()
    validate_startup_config()
    if not config.UPSTREAM_API_KEY:
        log.warning("UPSTREAM_API_KEY is not set — the LLM gateway will answer 503")

    app = FastAPI(
        title="deepseek-harness-cloud",
        docs_url="/api/docs" if config.DEV_MODE else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if config.DEV_MODE else None,
    )

    from .http_limits import RequestBodyLimit

    app.add_middleware(
        RequestBodyLimit,
        default_bytes=config.API_BODY_MAX_BYTES,
        webhook_bytes=config.WEBHOOK_BODY_MAX_BYTES,
    )

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

    from .security_headers import SecurityHeaders

    app.add_middleware(
        SecurityHeaders, https=config.PUBLIC_BASE.startswith("https://"), work_host=config.WORK_DOMAIN
    )

    from .accounts import router as accounts_router
    from .admin import router as admin_router
    from .desktop_updates import router as updates_router
    from .device_auth import router as device_router
    from .gateway import router as gateway_router
    from .health import router as health_router
    from .media import router as media_router
    from .oauth import router as oauth_router
    from .payments.api import router as payments_router
    from .teams import router as teams_router
    from .webpages import router as pages_router
    from .workspace import preview_fallback as workspace_preview_fallback
    from .workspace import router as workspace_router

    app.include_router(accounts_router)
    app.include_router(health_router)
    app.include_router(oauth_router)
    app.include_router(device_router)
    app.include_router(gateway_router)
    app.include_router(media_router)
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

    # 视频作业的服务端兜底。**不挂在 WORK_ENABLED 下面**: 媒体端点不依赖云工作台
    # (可以只用 API), 而钱是提交时就扣的 —— 没有这个循环, 客户端一走那笔钱就
    # 永远悬着: 失败不退款, 成功不记账。
    if config.UPSTREAM_API_KEY:
        from .media import reconcile_loop

        @app.on_event("startup")
        async def _start_media_loop() -> None:
            import asyncio

            app.state.media_loop = asyncio.create_task(reconcile_loop())

    # Report a bad offline-volume mount at startup instead of silently rendering
    # empty files for stopped workspaces.
    if config.WORK_VOLUME_ROOT and not Path(config.WORK_VOLUME_ROOT).is_dir():
        log.warning(
            "WORK_VOLUME_ROOT=%s is not a directory in this container — "
            "个人成品 will be empty whenever a workspace is stopped. "
            "Check DOCKER_VOLUME_ROOT in .env against "
            "`docker info -f '{{.DockerRootDir}}'`/volumes on the host.",
            config.WORK_VOLUME_ROOT,
        )

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

    @app.middleware("http")
    async def preview_origin_isolation(request, call_next):
        """智能体生成的内容与会话源分开, 两个方向都要管。

        向内: 预览域上不提供 API。否则智能体写的页面对着自己的源就能带凭据调
              我们的接口 —— 而同源请求连 Origin 白名单那道闸都不会触发。
        向外: 主站上不提供智能体内容, 改为 307 到预览域。旧链接、书签、以及
              页面里残留的相对路径都会被顺过去。

        PREVIEW_DOMAIN 留空时整段是空操作, 行为与配置前完全一致。
        """
        from . import workspace as _ws

        if config.PREVIEW_DOMAIN:
            path = request.url.path
            if _ws.on_preview_host(request):
                if path.startswith("/api/"):
                    return JSONResponse(status_code=404, content={"detail": "not_found"})
            elif _ws.is_agent_content(path):
                q = ("?" + request.url.query) if request.url.query else ""
                return RedirectResponse(_ws.preview_origin(path) + q, status_code=307)
        return await call_next(request)

    # Registered after every real route, so it only ever sees requests that
    # would otherwise 404: a previewed page asking for an absolute-path asset
    # ("/style.css") from outside its /preview/<port>/ prefix. The preview
    # cookie says which port that page came from.
    app.add_route("/{path:path}", workspace_preview_fallback, methods=["GET", "HEAD"])

    return app


app = create_app()
