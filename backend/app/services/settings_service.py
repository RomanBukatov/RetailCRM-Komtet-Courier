"""Persistent settings & mappings management, seeding, and runtime lookup."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Mapping, Setting
from app.schemas import (
    MappingItemSchema,
    SettingItemSchema,
    SettingsResponse,
    SettingsUpdateRequest,
)
from app.security import validate_outbound_url


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved runtime configuration combining DB settings and `.env` fallback."""

    retailcrm_url: str
    retailcrm_api_key: str
    retailcrm_default_site: str
    komtet_base_url: str
    komtet_shop_id: str
    komtet_secret_key: str
    komtet_queue_id: Optional[int]
    komtet_api_version: Literal["v1", "v2"]
    tax_system: int
    default_vat: str
    payment_address: str
    company_inn: str
    trigger_statuses: list[str]
    cancel_statuses: list[str]
    final_status: str
    default_delivery_time_from: str
    default_delivery_time_to: str
    komtet_callback_url: str
    verify_komtet_signature: bool
    anti_loop_seconds: int
    auto_sync_enabled: bool
    komtet_custom_field_code: str
    app_timezone: str = "Europe/Moscow"


@dataclass(frozen=True)
class MappingLookup:
    """Fast lookup tables for active RetailCRM <-> KOMTET mappings."""

    couriers_crm_to_komtet: dict[str, str]
    payments_crm_to_komtet: dict[str, str]
    payments_komtet_to_crm: dict[str, str]
    vat_crm_to_komtet: dict[str, str]


DEFAULT_TRIGGER_STATUSES = ["new", "paid", "send-to-delivery", "delivering"]
DEFAULT_CANCEL_STATUSES = [
    "cancel-other",
    "no-product",
    "delyvery-did-not-suit",
    "neudacha",
    "otklonen",
    "ozhidaet-podtverzhdeniia-zakaza",
]

# Metadata for all known setting keys: (value_type, is_secret, description, default_value)
SETTING_DEFINITIONS: dict[str, tuple[str, bool, str, Any]] = {
    "retailcrm_url": ("string", False, "URL аккаунта RetailCRM", ""),
    "retailcrm_api_key": ("secret", True, "API-ключ RetailCRM", ""),
    "retailcrm_default_site": ("string", False, "Код сайта RetailCRM по умолчанию", ""),
    "komtet_base_url": ("string", False, "Базовый URL КОМТЕТ Касса", "https://kassa.komtet.ru"),
    "komtet_shop_id": ("string", False, "Shop ID в КОМТЕТ Кассе", ""),
    "komtet_secret_key": ("secret", True, "Secret Key в КОМТЕТ Кассе", ""),
    "komtet_queue_id": ("int", False, "ID очереди (кассы) КОМТЕТ", None),
    "komtet_api_version": ("string", False, "Версия API КОМТЕТ (v1/v2)", "v2"),
    "tax_system": ("int", False, "Система налогообложения (0=ОСНО, 1=УСН д, 2=УСН д-р, 4=ЕСХН, 5=Патент)", 0),
    "default_vat": ("string", False, "Ставка НДС по умолчанию для КОМТЕТ", "22"),
    "payment_address": ("string", False, "Платёжный адрес (сайт магазина)", ""),
    "company_inn": ("string", False, "ИНН организации/ИП", ""),
    "trigger_statuses": ("json", False, "Статусы RetailCRM для отправки в КОМТЕТ", DEFAULT_TRIGGER_STATUSES),
    "cancel_statuses": ("json", False, "Статусы отмены RetailCRM", DEFAULT_CANCEL_STATUSES),
    "final_status": ("string", False, "Финальный статус в RetailCRM после оплаты/чека", "complete"),
    "default_delivery_time_from": ("string", False, "Время доставки по умолчанию (с)", "09:00"),
    "default_delivery_time_to": ("string", False, "Время доставки по умолчанию (до)", "21:00"),
    "komtet_callback_url": ("string", False, "Публичный URL для callback от КОМТЕТ", ""),
    "verify_komtet_signature": ("bool", False, "Проверять X-HMAC-Signature от КОМТЕТ", True),
    "anti_loop_seconds": ("int", False, "Окно защиты от петли после callback (сек)", 15),
    "auto_sync_enabled": ("bool", False, "Автоматическая синхронизация по вебхукам", True),
    "komtet_custom_field_code": ("string", False, "Код доп. поля ID заказа КОМТЕТ в RetailCRM", "komtet_id"),
}

DEFAULT_MAPPINGS: list[dict[str, Any]] = [
    {
        "mapping_type": "payment_type",
        "retailcrm_code": "cash",
        "retailcrm_name": "Наличные",
        "komtet_code": "cash",
        "komtet_name": "Наличными",
    },
    {
        "mapping_type": "payment_type",
        "retailcrm_code": "bank-card",
        "retailcrm_name": "Банковская карта",
        "komtet_code": "card",
        "komtet_name": "Электронными (карта)",
    },
    {
        "mapping_type": "payment_type",
        "retailcrm_code": "oplachen",
        "retailcrm_name": "Оплачен",
        "komtet_code": "prepayment",
        "komtet_name": "Предоплата",
    },
    {
        "mapping_type": "vat",
        "retailcrm_code": "10",
        "retailcrm_name": "НДС 10%",
        "komtet_code": "10",
        "komtet_name": "10%",
    },
    {
        "mapping_type": "vat",
        "retailcrm_code": "22",
        "retailcrm_name": "НДС 22%",
        "komtet_code": "22",
        "komtet_name": "22%",
    },
    {
        "mapping_type": "vat",
        "retailcrm_code": "20",
        "retailcrm_name": "НДС 20%",
        "komtet_code": "20",
        "komtet_name": "20%",
    },
    {
        "mapping_type": "vat",
        "retailcrm_code": "0",
        "retailcrm_name": "НДС 0%",
        "komtet_code": "0",
        "komtet_name": "0%",
    },
    {
        "mapping_type": "vat",
        "retailcrm_code": "none",
        "retailcrm_name": "Без НДС",
        "komtet_code": "no",
        "komtet_name": "Без НДС",
    },
]


def mask_secret(value: Optional[str]) -> str:
    """Return masked representation of a secret string, preserving last 4 chars."""
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def _serialize_setting_value(value: Any, value_type: str) -> Optional[str]:
    if value is None:
        return None
    if value_type == "json":
        return json.dumps(value, ensure_ascii=False)
    if value_type == "bool":
        return "true" if bool(value) else "false"
    return str(value)


def _deserialize_setting_value(raw: Optional[str], value_type: str, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if value_type == "int":
        try:
            return int(raw)
        except ValueError:
            return default
    if value_type == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if value_type == "json":
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


async def seed_defaults(
    session: AsyncSession,
    env_settings: Optional[Settings] = None,
) -> None:
    """Idempotently populate default settings and reference mappings."""
    env = env_settings or get_settings()

    existing_keys_result = await session.execute(select(Setting.key))
    existing_keys = set(existing_keys_result.scalars().all())

    env_overrides: dict[str, Any] = {
        "retailcrm_url": env.retailcrm_url,
        "retailcrm_api_key": env.retailcrm_api_key.get_secret_value(),
        "retailcrm_default_site": env.retailcrm_default_site,
        "komtet_base_url": env.komtet_base_url,
        "komtet_shop_id": env.komtet_shop_id,
        "komtet_secret_key": env.komtet_secret_key.get_secret_value(),
        "komtet_queue_id": env.komtet_queue_id,
        "komtet_api_version": env.komtet_api_version,
        "komtet_callback_url": env.resolved_komtet_callback_url,
    }

    for key, (value_type, is_secret, description, default_val) in SETTING_DEFINITIONS.items():
        if key in existing_keys:
            continue
        initial_value = env_overrides.get(key)
        if initial_value is None or initial_value == "":
            initial_value = default_val
        session.add(
            Setting(
                key=key,
                value=_serialize_setting_value(initial_value, value_type),
                value_type=value_type,
                is_secret=is_secret,
                description=description,
            )
        )

    mapping_count = await session.scalar(select(func.count(Mapping.id)))
    if not mapping_count:
        for item in DEFAULT_MAPPINGS:
            session.add(Mapping(**item, is_active=True))

    await session.commit()


async def load_runtime_config(
    session: AsyncSession,
    env_settings: Optional[Settings] = None,
) -> RuntimeConfig:
    """Resolve current RuntimeConfig from DB settings with `.env` fallback."""
    env = env_settings or get_settings()
    rows = (await session.execute(select(Setting))).scalars().all()
    db_map = {row.key: row for row in rows}

    def get_val(key: str, env_fallback: Any = None) -> Any:
        value_type, _, _, default_val = SETTING_DEFINITIONS[key]
        row = db_map.get(key)
        if row is not None and row.value is not None and row.value != "":
            return _deserialize_setting_value(row.value, value_type, default_val)
        if env_fallback not in (None, ""):
            return env_fallback
        return default_val

    api_ver_raw = str(get_val("komtet_api_version", env.komtet_api_version)).lower()
    api_version: Literal["v1", "v2"] = "v1" if api_ver_raw == "v1" else "v2"

    trigger_statuses = get_val("trigger_statuses")
    if not isinstance(trigger_statuses, list):
        trigger_statuses = list(DEFAULT_TRIGGER_STATUSES)

    cancel_statuses = get_val("cancel_statuses")
    if not isinstance(cancel_statuses, list):
        cancel_statuses = list(DEFAULT_CANCEL_STATUSES)

    return RuntimeConfig(
        retailcrm_url=str(get_val("retailcrm_url", env.retailcrm_url)).rstrip("/"),
        retailcrm_api_key=str(
            get_val("retailcrm_api_key", env.retailcrm_api_key.get_secret_value())
        ),
        retailcrm_default_site=str(
            get_val("retailcrm_default_site", env.retailcrm_default_site)
        ).strip(),
        komtet_base_url=str(get_val("komtet_base_url", env.komtet_base_url)).rstrip("/"),
        komtet_shop_id=str(get_val("komtet_shop_id", env.komtet_shop_id)),
        komtet_secret_key=str(
            get_val("komtet_secret_key", env.komtet_secret_key.get_secret_value())
        ),
        komtet_queue_id=get_val("komtet_queue_id", env.komtet_queue_id),
        komtet_api_version=api_version,
        tax_system=int(get_val("tax_system")),
        default_vat=str(get_val("default_vat")),
        payment_address=str(get_val("payment_address")),
        company_inn=str(get_val("company_inn")),
        trigger_statuses=[str(s).strip() for s in trigger_statuses if str(s).strip()],
        cancel_statuses=[str(s).strip() for s in cancel_statuses if str(s).strip()],
        final_status=str(get_val("final_status")),
        default_delivery_time_from=str(get_val("default_delivery_time_from")),
        default_delivery_time_to=str(get_val("default_delivery_time_to")),
        komtet_callback_url=str(
            get_val("komtet_callback_url", env.resolved_komtet_callback_url)
        ).strip(),
        verify_komtet_signature=bool(get_val("verify_komtet_signature")),
        anti_loop_seconds=int(get_val("anti_loop_seconds")),
        auto_sync_enabled=bool(get_val("auto_sync_enabled")),
        komtet_custom_field_code=str(get_val("komtet_custom_field_code")) or "komtet_id",
        app_timezone=env.app_timezone,
    )


async def get_settings_response(
    session: AsyncSession,
    env_settings: Optional[Settings] = None,
) -> SettingsResponse:
    """Build masked SettingsResponse for the admin API."""
    cfg = await load_runtime_config(session, env_settings=env_settings)
    rows = (await session.execute(select(Setting).order_by(Setting.key))).scalars().all()
    db_map = {row.key: row for row in rows}

    items: list[SettingItemSchema] = []
    for key, (value_type, is_secret, description, default_val) in SETTING_DEFINITIONS.items():
        row = db_map.get(key)
        raw_val = getattr(cfg, key, default_val)
        display_val = mask_secret(str(raw_val)) if is_secret else raw_val
        items.append(
            SettingItemSchema(
                key=key,
                value=display_val,
                value_type=value_type,
                is_secret=is_secret,
                is_set=bool(raw_val) if is_secret else raw_val is not None,
                description=row.description if row and row.description else description,
                updated_at=row.updated_at if row else None,
            )
        )

    return SettingsResponse(
        retailcrm_url=cfg.retailcrm_url,
        retailcrm_api_key_masked=mask_secret(cfg.retailcrm_api_key),
        has_retailcrm_api_key=bool(cfg.retailcrm_api_key),
        retailcrm_default_site=cfg.retailcrm_default_site,
        komtet_base_url=cfg.komtet_base_url,
        komtet_shop_id=cfg.komtet_shop_id,
        komtet_secret_key_masked=mask_secret(cfg.komtet_secret_key),
        has_komtet_secret_key=bool(cfg.komtet_secret_key),
        komtet_queue_id=cfg.komtet_queue_id,
        komtet_api_version=cfg.komtet_api_version,
        tax_system=cfg.tax_system,
        default_vat=cfg.default_vat,
        payment_address=cfg.payment_address,
        company_inn=cfg.company_inn,
        trigger_statuses=cfg.trigger_statuses,
        cancel_statuses=cfg.cancel_statuses,
        final_status=cfg.final_status,
        default_delivery_time_from=cfg.default_delivery_time_from,
        default_delivery_time_to=cfg.default_delivery_time_to,
        komtet_callback_url=cfg.komtet_callback_url,
        verify_komtet_signature=cfg.verify_komtet_signature,
        anti_loop_seconds=cfg.anti_loop_seconds,
        auto_sync_enabled=cfg.auto_sync_enabled,
        komtet_custom_field_code=cfg.komtet_custom_field_code,
        items=items,
    )


async def update_settings(
    session: AsyncSession,
    payload: SettingsUpdateRequest,
    env_settings: Optional[Settings] = None,
) -> SettingsResponse:
    """Persist settings updates to DB without overwriting secrets with masked placeholders."""
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return await get_settings_response(session, env_settings=env_settings)

    env = env_settings or get_settings()
    allow_http = env.app_env == "test"

    rows = (
        await session.execute(select(Setting).where(Setting.key.in_(list(updates.keys()))))
    ).scalars().all()
    db_map = {row.key: row for row in rows}

    for key, new_val in updates.items():
        if key not in SETTING_DEFINITIONS:
            continue
        value_type, is_secret, description, _ = SETTING_DEFINITIONS[key]

        if is_secret:
            str_val = str(new_val).strip()
            # Skip masked placeholder or blank submission so existing key stays intact.
            if not str_val or str_val.startswith("****"):
                continue
            new_val = str_val
        elif key in {"retailcrm_url", "komtet_base_url", "komtet_callback_url"}:
            new_val = validate_outbound_url(
                str(new_val),
                allow_http=allow_http,
                field_name=key,
            )

        serialized = _serialize_setting_value(new_val, value_type)
        row = db_map.get(key)
        if row is None:
            session.add(
                Setting(
                    key=key,
                    value=serialized,
                    value_type=value_type,
                    is_secret=is_secret,
                    description=description,
                )
            )
        else:
            row.value = serialized

    await session.commit()
    return await get_settings_response(session, env_settings=env_settings)


async def load_active_mappings(session: AsyncSession) -> MappingLookup:
    """Load active mappings into directional lookup dictionaries."""
    rows = (
        await session.execute(
            select(Mapping).where(Mapping.is_active.is_(True)).order_by(Mapping.id)
        )
    ).scalars().all()

    couriers_crm_to_komtet: dict[str, str] = {}
    payments_crm_to_komtet: dict[str, str] = {}
    payments_komtet_to_crm: dict[str, str] = {}
    vat_crm_to_komtet: dict[str, str] = {}

    for row in rows:
        if row.mapping_type == "courier":
            couriers_crm_to_komtet[row.retailcrm_code] = row.komtet_code
        elif row.mapping_type == "payment_type":
            payments_crm_to_komtet[row.retailcrm_code] = row.komtet_code
            # Preserve first mapping when multiple CRM payment types share a KOMTET code.
            payments_komtet_to_crm.setdefault(row.komtet_code, row.retailcrm_code)
        elif row.mapping_type == "vat":
            vat_crm_to_komtet[row.retailcrm_code.strip().lower()] = row.komtet_code

    return MappingLookup(
        couriers_crm_to_komtet=couriers_crm_to_komtet,
        payments_crm_to_komtet=payments_crm_to_komtet,
        payments_komtet_to_crm=payments_komtet_to_crm,
        vat_crm_to_komtet=vat_crm_to_komtet,
    )


async def list_mappings(
    session: AsyncSession,
    mapping_type: Optional[str] = None,
) -> list[Mapping]:
    """Return persisted mappings optionally filtered by mapping_type."""
    stmt = select(Mapping).order_by(Mapping.mapping_type, Mapping.id)
    if mapping_type:
        stmt = stmt.where(Mapping.mapping_type == mapping_type)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def upsert_mappings(
    session: AsyncSession,
    items: list[MappingItemSchema],
    replace_types: Optional[list[str]] = None,
) -> list[Mapping]:
    """Bulk upsert mappings and optionally replace full sets for specified mapping_types."""
    types_to_replace = set(replace_types or [])
    if types_to_replace:
        await session.execute(
            delete(Mapping).where(Mapping.mapping_type.in_(list(types_to_replace)))
        )
        await session.flush()

    for item in items:
        existing = await session.scalar(
            select(Mapping).where(
                Mapping.mapping_type == item.mapping_type,
                Mapping.retailcrm_code == item.retailcrm_code,
            )
        )
        if existing is None:
            session.add(
                Mapping(
                    mapping_type=item.mapping_type,
                    retailcrm_code=item.retailcrm_code,
                    retailcrm_name=item.retailcrm_name,
                    komtet_code=item.komtet_code,
                    komtet_name=item.komtet_name,
                    is_active=item.is_active,
                    extra=item.extra,
                )
            )
        else:
            existing.retailcrm_name = item.retailcrm_name
            existing.komtet_code = item.komtet_code
            existing.komtet_name = item.komtet_name
            existing.is_active = item.is_active
            existing.extra = item.extra

    await session.commit()
    return await list_mappings(session)
