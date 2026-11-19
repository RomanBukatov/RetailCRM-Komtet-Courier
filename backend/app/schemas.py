"""Pydantic v2 request and response schemas for REST API and webhooks."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

_POSITIVE_INT_RE = re.compile(r"^\d+$")


def _ensure_utc_datetime(value: datetime) -> datetime:
    """Attach UTC timezone to naive SQLite datetimes and normalize aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime, AfterValidator(_ensure_utc_datetime)]


class SettingItemSchema(BaseModel):
    """Single setting entry returned to the admin UI."""

    key: str
    value: Any
    value_type: str
    is_secret: bool = False
    is_set: bool = True
    description: Optional[str] = None
    updated_at: Optional[UtcDateTime] = None


class SettingsResponse(BaseModel):
    """Structured settings payload for GET/PUT /api/v1/settings."""

    retailcrm_url: str = ""
    retailcrm_api_key_masked: str = ""
    has_retailcrm_api_key: bool = False
    retailcrm_default_site: str = ""
    komtet_base_url: str = "https://kassa.komtet.ru"
    komtet_shop_id: str = ""
    komtet_secret_key_masked: str = ""
    has_komtet_secret_key: bool = False
    komtet_queue_id: Optional[int] = None
    komtet_api_version: Literal["v1", "v2"] = "v2"
    tax_system: int = 0
    default_vat: str = "22"
    payment_address: str = ""
    company_inn: str = ""
    trigger_statuses: list[str] = Field(default_factory=list)
    cancel_statuses: list[str] = Field(default_factory=list)
    final_status: str = "complete"
    default_delivery_time_from: str = "09:00"
    default_delivery_time_to: str = "21:00"
    komtet_callback_url: str = ""
    verify_komtet_signature: bool = True
    anti_loop_seconds: int = 15
    auto_sync_enabled: bool = True
    komtet_custom_field_code: str = "komtet_id"
    items: list[SettingItemSchema] = Field(default_factory=list)


class SettingsUpdateRequest(BaseModel):
    """Partial or full update payload for PUT /api/v1/settings."""

    retailcrm_url: Optional[str] = None
    retailcrm_api_key: Optional[str] = None
    retailcrm_default_site: Optional[str] = None
    komtet_base_url: Optional[str] = None
    komtet_shop_id: Optional[str] = None
    komtet_secret_key: Optional[str] = None
    komtet_queue_id: Optional[int] = Field(default=None, ge=1)
    komtet_api_version: Optional[Literal["v1", "v2"]] = None
    tax_system: Optional[int] = Field(default=None, ge=0, le=5)
    default_vat: Optional[str] = None
    payment_address: Optional[str] = None
    company_inn: Optional[str] = None
    trigger_statuses: Optional[list[str]] = None
    cancel_statuses: Optional[list[str]] = None
    final_status: Optional[str] = None
    default_delivery_time_from: Optional[str] = None
    default_delivery_time_to: Optional[str] = None
    komtet_callback_url: Optional[str] = None
    verify_komtet_signature: Optional[bool] = None
    anti_loop_seconds: Optional[int] = Field(default=None, ge=0, le=3600)
    auto_sync_enabled: Optional[bool] = None
    komtet_custom_field_code: Optional[str] = None


class ConnectionCheckResult(BaseModel):
    """Status of RetailCRM and KOMTET connectivity and custom field setup."""

    retailcrm_ok: bool
    retailcrm_message: str
    retailcrm_sites: list[str] = Field(default_factory=list)
    komtet_field_exists: bool = False
    komtet_field_created: bool = False
    komtet_ok: bool
    komtet_message: str
    komtet_queue_active: Optional[bool] = None
    komtet_delivery_service_active: Optional[bool] = None
    warnings: list[str] = Field(default_factory=list)


class MappingItemSchema(BaseModel):
    """Single RetailCRM <-> KOMTET mapping record."""

    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    mapping_type: str
    retailcrm_code: str
    retailcrm_name: Optional[str] = None
    komtet_code: str
    komtet_name: Optional[str] = None
    is_active: bool = True
    extra: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def _validate_courier_komtet_code(self) -> "MappingItemSchema":
        if self.mapping_type == "courier":
            cleaned = (self.komtet_code or "").strip()
            if cleaned:
                if not _POSITIVE_INT_RE.fullmatch(cleaned) or int(cleaned) <= 0:
                    raise ValueError(
                        "Для маппинга курьера komtet_code должен быть целым положительным числом"
                    )
                self.komtet_code = cleaned
        return self


class MappingsResponse(BaseModel):
    """List of mappings returned by GET /api/v1/mappings."""

    items: list[MappingItemSchema]


class MappingsUpdateRequest(BaseModel):
    """Bulk upsert/replace request for PUT /api/v1/mappings."""

    items: list[MappingItemSchema]
    replace_types: list[str] = Field(
        default_factory=list,
        description="Mapping types to replace completely with the provided items.",
    )


class DictionariesResponse(BaseModel):
    """Live dictionaries fetched from RetailCRM and KOMTET with graceful warnings."""

    retailcrm_statuses: list[dict[str, Any]] = Field(default_factory=list)
    retailcrm_payment_types: list[dict[str, Any]] = Field(default_factory=list)
    retailcrm_couriers: list[dict[str, Any]] = Field(default_factory=list)
    retailcrm_sites: list[dict[str, Any]] = Field(default_factory=list)
    komtet_employees: list[dict[str, Any]] = Field(default_factory=list)
    komtet_couriers: list[dict[str, Any]] = Field(default_factory=list)
    komtet_queue: Optional[dict[str, Any]] = None
    warnings: list[str] = Field(default_factory=list)


class SyncLogItemSchema(BaseModel):
    """Single sync journal entry."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    direction: str
    level: str
    event_type: str
    retailcrm_order_id: Optional[int] = None
    komtet_order_id: Optional[int] = None
    message: str
    payload: Optional[dict[str, Any]] = None
    error_details: Optional[str] = None
    created_at: UtcDateTime


class SyncLogsResponse(BaseModel):
    """Paginated sync logs response."""

    items: list[SyncLogItemSchema]
    total: int
    limit: int
    offset: int


class OrderSyncItemSchema(BaseModel):
    """Bidirectional order sync linkage state."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    retailcrm_order_id: int
    retailcrm_order_number: Optional[str] = None
    retailcrm_site: Optional[str] = None
    komtet_order_id: Optional[int] = None
    komtet_external_id: Optional[str] = None
    komtet_state: Optional[str] = None
    sync_status: str
    last_hash: Optional[str] = None
    last_error: Optional[str] = None
    last_synced_at: Optional[UtcDateTime] = None
    last_komtet_callback_at: Optional[UtcDateTime] = None
    updated_at: UtcDateTime


class OrderSyncListResponse(BaseModel):
    """Paginated list of order sync records."""

    items: list[OrderSyncItemSchema]
    total: int
    limit: int
    offset: int
