"""SQLAlchemy 2.0 ORM models for configuration, mappings, sync state, and logs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


class Setting(Base):
    """Persistent service settings (API credentials, SNO, trigger statuses, defaults)."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    value_type: Mapped[str] = mapped_column(String(32), default="string", nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        display_value = "***" if self.is_secret and self.value else self.value
        return f"<Setting key={self.key!r} value={display_value!r}>"


class Mapping(Base):
    """Reference mappings between RetailCRM and KOMTET (couriers, payments, VAT, etc.)."""

    __tablename__ = "mappings"
    __table_args__ = (
        UniqueConstraint(
            "mapping_type",
            "retailcrm_code",
            name="uq_mappings_type_retailcrm_code",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapping_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    retailcrm_code: Mapped[str] = mapped_column(String(128), nullable=False)
    retailcrm_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    komtet_code: Mapped[str] = mapped_column(String(128), nullable=False)
    komtet_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extra: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<Mapping type={self.mapping_type!r} "
            f"{self.retailcrm_code!r}->{self.komtet_code!r}>"
        )


class OrderSync(Base):
    """Idempotency and bidirectional linkage table between RetailCRM and KOMTET orders."""

    __tablename__ = "order_sync"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    retailcrm_order_id: Mapped[int] = mapped_column(
        Integer, unique=True, index=True, nullable=False
    )
    retailcrm_order_number: Mapped[Optional[str]] = mapped_column(
        String(64), index=True, nullable=True
    )
    retailcrm_site: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    komtet_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, unique=True, index=True, nullable=True
    )
    komtet_external_id: Mapped[Optional[str]] = mapped_column(
        String(128), index=True, nullable=True
    )
    komtet_state: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sync_status: Mapped[str] = mapped_column(
        String(32), default="pending", index=True, nullable=False
    )
    last_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_komtet_callback_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_komtet_callback_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
        server_default=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<OrderSync crm_id={self.retailcrm_order_id} "
            f"komtet_id={self.komtet_order_id} status={self.sync_status!r}>"
        )


class SyncLog(Base):
    """Audit journal of synchronization events, webhook callbacks, and errors."""

    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    direction: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="INFO", index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    retailcrm_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, index=True, nullable=True
    )
    komtet_order_id: Mapped[Optional[int]] = mapped_column(
        Integer, index=True, nullable=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, server_default=func.now(), index=True, nullable=False
    )

    def __repr__(self) -> str:
        return f"<SyncLog id={self.id} level={self.level!r} event={self.event_type!r}>"
