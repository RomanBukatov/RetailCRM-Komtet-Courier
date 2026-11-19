"""FastAPI application entry point for RetailCRM <-> KOMTET Kassa integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import cleanup_old_logs, engine, get_db, init_db
from app.routers.admin_api import router as admin_router
from app.routers.webhooks import router as webhooks_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

_LOG_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
_LOG_CLEANUP_RETRY_SECONDS = 600


async def _log_retention_loop(retention_days: int) -> None:
    """Periodically purge old sync_logs records and compact SQLite WAL."""
    while True:
        sleep_seconds = _LOG_CLEANUP_INTERVAL_SECONDS
        try:
            deleted = await cleanup_old_logs(retention_days)
            logger.info(
                "Log retention cleanup complete (retention_days=%d, deleted=%d)",
                retention_days,
                deleted,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Log retention cleanup failed; retrying in %d seconds",
                _LOG_CLEANUP_RETRY_SECONDS,
            )
            sleep_seconds = _LOG_CLEANUP_RETRY_SECONDS
        await asyncio.sleep(sleep_seconds)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize database schema and seed defaults on startup; dispose engine on shutdown."""
    cfg: Settings = getattr(application.state, "settings", settings)
    logger.info("Starting %s (env=%s)", cfg.app_name, cfg.app_env)
    await init_db(seed=True, env_settings=cfg)
    cleanup_task: asyncio.Task[None] | None = None
    if cfg.log_retention_days > 0:
        cleanup_task = asyncio.create_task(
            _log_retention_loop(cfg.log_retention_days),
            name="sync_logs_retention_cleanup",
        )
    else:
        logger.info(
            "Log retention cleanup disabled (LOG_RETENTION_DAYS=%d)",
            cfg.log_retention_days,
        )
    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
        await engine.dispose()
        logger.info("Shutdown complete")


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    cfg = app_settings or get_settings()

    application = FastAPI(
        title=cfg.app_name,
        version="0.2.0",
        description="Two-way integration service between RetailCRM and KOMTET Kassa Courier",
        lifespan=lifespan,
        docs_url=None if cfg.is_production else "/docs",
        redoc_url=None if cfg.is_production else "/redoc",
        openapi_url=None if cfg.is_production else "/openapi.json",
    )
    application.state.settings = cfg

    if cfg.is_production:
        if not cfg.admin_api_token.get_secret_value():
            logger.error("ADMIN_API_TOKEN is empty: admin API will reject all requests (503)")
        if not cfg.retailcrm_webhook_token.get_secret_value():
            logger.error("RETAILCRM_WEBHOOK_TOKEN is empty: RetailCRM webhook will reject all requests (503)")
    else:
        if not cfg.admin_api_token.get_secret_value():
            logger.warning("ADMIN_API_TOKEN is empty: admin API is UNAUTHENTICATED (allowed outside production only)")

    cors_origins = cfg.effective_cors_origins
    if cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    application.include_router(webhooks_router)
    application.include_router(admin_router)

    @application.get("/health", tags=["system"])
    async def health() -> dict[str, str]:
        """Liveness probe endpoint."""
        return {"status": "ok", "service": "komtet-retailcrm"}

    @application.get("/api/v1/health", tags=["system"])
    async def detailed_health(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
        """Readiness check verifying database connectivity and config flags."""
        await db.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "database": "connected",
            "retailcrm_configured": cfg.is_retailcrm_configured,
            "komtet_configured": cfg.is_komtet_configured,
            "komtet_api_version": cfg.komtet_api_version,
        }

    return application


app = create_app(settings)
