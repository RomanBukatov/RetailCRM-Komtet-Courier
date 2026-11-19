"""FastAPI application entry point for RetailCRM <-> KOMTET Kassa integration."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import engine, get_db, init_db
from app.routers.admin_api import router as admin_router
from app.routers.webhooks import router as webhooks_router

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize database schema and seed defaults on startup; dispose engine on shutdown."""
    logger.info("Starting %s (env=%s)", settings.app_name, settings.app_env)
    await init_db(seed=True)
    yield
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

    application.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
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
