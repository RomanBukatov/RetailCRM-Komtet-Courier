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
from app.database import async_session_factory, cleanup_old_logs, engine, get_db, init_db
from app.routers.admin_api import router as admin_router
from app.routers.webhooks import router as webhooks_router
from app.services.sync_service import run_reconciliation_cycle

settings = get_settings()

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _configure_logging(log_level: str) -> None:
    """Configure unified compact formatter on root stream handlers and `app.*` loggers."""
    raw_level = getattr(logging, (log_level or "INFO").upper(), logging.INFO)
    level = raw_level if isinstance(raw_level, int) else logging.INFO
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATEFMT)

    root_logger = logging.getLogger()
    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)

    # Match exact StreamHandler so pytest's LogCaptureHandler (a subclass) is left untouched.
    stream_handlers = [h for h in root_logger.handlers if type(h) is logging.StreamHandler]
    if stream_handlers:
        for handler in stream_handlers:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    logging.getLogger("app").setLevel(level)
    logging.getLogger("httpx").setLevel(max(level, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(level, logging.WARNING))


_configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

_LOG_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
_LOG_CLEANUP_RETRY_SECONDS = 600


class _HealthCheckAccessFilter(logging.Filter):
    """Drop successful liveness probe entries from uvicorn.access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 5:
            method, path, status_code = record.args[1], record.args[2], record.args[4]
            if method == "GET" and path == "/health" and status_code == 200:
                return False
        msg = record.getMessage()
        return not ('"GET /health ' in msg and msg.rstrip().endswith("200"))


def _install_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HealthCheckAccessFilter) for f in access_logger.filters):
        access_logger.addFilter(_HealthCheckAccessFilter())


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


async def _reconciliation_loop(cfg: Settings) -> None:
    interval = cfg.reconcile_interval_seconds
    logger.info("Reconciliation worker started (interval=%d seconds)", interval)
    after_ids = (0, 0)
    while True:
        await asyncio.sleep(interval)
        try:
            after_ids = await run_reconciliation_cycle(async_session_factory, env_settings=cfg, after_ids=after_ids)
        except Exception:
            logger.exception("Reconciliation cycle failed")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize database schema and seed defaults on startup; dispose engine on shutdown."""
    cfg: Settings = getattr(application.state, "settings", settings)
    _configure_logging(cfg.log_level)
    _install_access_log_filter()
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
    reconciliation_task = None
    if cfg.app_env != "test" and cfg.reconcile_interval_seconds > 0:
        reconciliation_task = asyncio.create_task(_reconciliation_loop(cfg), name="order_reconciliation")
    try:
        yield
    finally:
        tasks = [task for task in (cleanup_task, reconciliation_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await engine.dispose()
        logger.info("Shutdown complete")


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application instance."""
    cfg = app_settings or get_settings()
    _configure_logging(cfg.log_level)

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
