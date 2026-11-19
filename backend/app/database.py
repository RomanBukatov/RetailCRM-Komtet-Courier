"""Async SQLAlchemy 2.0 database engine, session factory, and helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base class for all ORM models."""


def _ensure_sqlite_directory(database_url: str) -> None:
    """Create parent directory for file-backed SQLite databases if missing."""
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return
    if url.database == ":memory:" or url.database.startswith("file:"):
        return

    db_path = Path(url.database)
    db_path.parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str | None = None) -> AsyncEngine:
    """Build an async SQLAlchemy engine configured for SQLite WAL or standard RDBMS."""
    resolved_url = database_url or get_settings().database_url
    _ensure_sqlite_directory(resolved_url)

    engine_kwargs: dict[str, Any] = {
        "echo": False,
        "future": True,
        "pool_pre_ping": True,
    }
    if resolved_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"timeout": 30}

    async_engine = create_async_engine(resolved_url, **engine_kwargs)

    if resolved_url.startswith("sqlite"):

        @event.listens_for(async_engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

    return async_engine


engine: AsyncEngine = create_engine()
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session per request with automatic cleanup."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db(
    target_engine: AsyncEngine | None = None,
    *,
    seed: bool = True,
    env_settings: Any = None,
) -> None:
    """Create database tables, apply lightweight SQLite migrations, and seed defaults."""
    # Ensure ORM models are imported so metadata includes all tables.
    import app.models  # noqa: F401
    from sqlalchemy import text

    active_engine = target_engine or engine
    async with active_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if active_engine.dialect.name == "sqlite":
            result = await conn.execute(text("PRAGMA table_info(order_sync)"))
            columns = {row[1] for row in result.fetchall()}
            if "last_komtet_callback_at" not in columns:
                await conn.execute(
                    text("ALTER TABLE order_sync ADD COLUMN last_komtet_callback_at DATETIME")
                )

    if seed:
        from app.services.settings_service import seed_defaults

        session_maker = async_sessionmaker(
            bind=active_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        async with session_maker() as session:
            await seed_defaults(session, env_settings=env_settings)
