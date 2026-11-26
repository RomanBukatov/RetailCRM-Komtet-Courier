"""Admin REST API endpoints for configuration, live dictionaries, mappings, and logs."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.komtet import KomtetClient, KomtetError
from app.clients.retailcrm import RetailCRMClient, RetailCRMError
from app.config import Settings
from app.database import get_db
from app.models import OrderSync, SyncLog
from app.schemas import (
    ConnectionCheckResult,
    DictionariesResponse,
    MappingItemSchema,
    MappingsResponse,
    MappingsUpdateRequest,
    OrderSyncItemSchema,
    OrderSyncListResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    SyncLogItemSchema,
    SyncLogsResponse,
)
from app.security import get_app_settings, get_client_ip, require_admin_token
from app.services.settings_service import (
    SETTING_DEFINITIONS,
    RuntimeConfig,
    get_settings_response,
    list_mappings,
    load_runtime_config,
    update_settings,
    upsert_mappings,
)
from app.services.sync_service import (
    build_komtet_client,
    build_retailcrm_client,
    sync_retailcrm_to_komtet,
)

logger = logging.getLogger(__name__)
_SECRET_SETTING_KEYS = frozenset({"retailcrm_api_key", "komtet_secret_key"})

router = APIRouter(
    prefix="/api/v1",
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
)


def get_retailcrm_factory() -> Callable[[RuntimeConfig], RetailCRMClient]:
    """Dependency returning RetailCRMClient factory (overridable in tests)."""
    return build_retailcrm_client


def get_komtet_factory() -> Callable[[RuntimeConfig], KomtetClient]:
    """Dependency returning KomtetClient factory (overridable in tests)."""
    return build_komtet_client


@router.get("/settings", response_model=SettingsResponse)
async def read_settings(
    db: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_app_settings),
) -> SettingsResponse:
    """Return current service settings with secrets masked."""
    return await get_settings_response(db, env_settings=app_settings)


@router.put("/settings", response_model=SettingsResponse)
async def save_settings(
    request: Request,
    payload: SettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_app_settings),
) -> SettingsResponse:
    """Update service settings in SQLite and record a redacted audit log entry."""
    client_ip = get_client_ip(request)
    before_cfg = await load_runtime_config(db, env_settings=app_settings)
    try:
        response = await update_settings(db, payload, env_settings=app_settings)
    except ValueError as exc:
        logger.warning(
            "[Admin] PUT /api/v1/settings rejected ip=%s: %s",
            client_ip,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    after_cfg = await load_runtime_config(db, env_settings=app_settings)
    submitted = payload.model_dump(exclude_unset=True)
    if "require_mark_code" in submitted:
        submitted.setdefault("marking_mode", after_cfg.marking_mode)
    changed_fields: list[str] = []
    changed_values: dict[str, Any] = {}

    for key, raw_val in submitted.items():
        if key not in SETTING_DEFINITIONS:
            continue
        is_secret = key in _SECRET_SETTING_KEYS or SETTING_DEFINITIONS[key][1]
        if is_secret:
            if raw_val is None:
                continue
            str_val = str(raw_val).strip()
            if not str_val or str_val.startswith("****"):
                continue
            changed_fields.append(key)
            changed_values[key] = "[UPDATED]"
        else:
            # Audit resolved settings, including the paid-status prepayment policy.
            before_val = getattr(before_cfg, key)
            after_val = getattr(after_cfg, key)
            if before_val != after_val:
                changed_fields.append(key)
                changed_values[key] = after_val

    db.add(
        SyncLog(
            direction="system",
            level="INFO",
            event_type="settings_updated",
            message="Обновлены настройки через панель управления",
            payload={
                "changed_fields": changed_fields,
                "values": changed_values,
                "client_ip": client_ip,
            },
        )
    )
    await db.commit()
    logger.info(
        "[Admin] PUT /api/v1/settings ip=%s changed_fields=%s",
        client_ip,
        changed_fields,
    )
    return response


@router.post("/settings/test-connection", response_model=ConnectionCheckResult)
async def test_connection_and_init_field(
    request: Request,
    db: AsyncSession = Depends(get_db),
    crm_factory: Callable[[RuntimeConfig], RetailCRMClient] = Depends(get_retailcrm_factory),
    komtet_factory: Callable[[RuntimeConfig], KomtetClient] = Depends(get_komtet_factory),
) -> ConnectionCheckResult:
    """Check RetailCRM & KOMTET credentials and ensure `komtet_id` custom field exists."""
    started_at = time.perf_counter()
    client_ip = get_client_ip(request)
    logger.info("[Admin] POST /api/v1/settings/test-connection started ip=%s", client_ip)

    cfg = await load_runtime_config(db)
    warnings: list[str] = []

    retailcrm_ok = False
    retailcrm_message = "RetailCRM credentials not configured"
    retailcrm_sites: list[str] = []
    field_exists = False
    field_created = False

    if cfg.retailcrm_url and cfg.retailcrm_api_key:
        try:
            async with crm_factory(cfg) as crm:
                creds = await crm.check_credentials()
                retailcrm_sites = list(creds.get("sitesAvailable") or [])
                field_res = await crm.ensure_komtet_id_field(
                    code=cfg.komtet_custom_field_code,
                    name=cfg.komtet_custom_field_code,
                    field_type="integer",
                    entity="order",
                )
                field_exists = True
                field_created = bool(field_res.get("created"))
                retailcrm_ok = True
                retailcrm_message = "RetailCRM connection verified"
        except (RetailCRMError, ValueError) as exc:
            retailcrm_message = str(exc)

    komtet_ok = False
    komtet_message = "KOMTET credentials not configured"
    queue_active: Optional[bool] = None
    delivery_service_active: Optional[bool] = None

    if cfg.komtet_shop_id and cfg.komtet_secret_key:
        try:
            async with komtet_factory(cfg) as komtet:
                if cfg.komtet_queue_id is not None:
                    queue_info = await komtet.get_queue(cfg.komtet_queue_id)
                    queue_active = queue_info.get("state") == "active"
                else:
                    await komtet.get_employees(start=0, limit=1)

                komtet_ok = True
                komtet_message = "KOMTET Kassa authentication verified"

                # Probe delivery orders service in read-only GET mode to detect SRV00 early.
                try:
                    await komtet.get_orders(start=0, limit=1)
                    delivery_service_active = True
                except KomtetError as exc:
                    if exc.code == "SRV00" or exc.status_code == 403:
                        delivery_service_active = False
                        warnings.append(
                            f"Услуга доставки/заказов КОМТЕТ не активирована ({exc.code or exc.status_code}): {exc.description or exc.message}"
                        )
                    else:
                        warnings.append(f"Предупреждение при проверке заказов КОМТЕТ: {exc}")
        except (KomtetError, ValueError) as exc:
            komtet_message = str(exc)

    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    logger.info(
        "[Admin] POST /api/v1/settings/test-connection done ip=%s retailcrm_ok=%s komtet_ok=%s "
        "queue_active=%s delivery_active=%s warnings=%d (%.0f ms)",
        client_ip,
        retailcrm_ok,
        komtet_ok,
        queue_active,
        delivery_service_active,
        len(warnings),
        elapsed_ms,
    )
    return ConnectionCheckResult(
        retailcrm_ok=retailcrm_ok,
        retailcrm_message=retailcrm_message,
        retailcrm_sites=retailcrm_sites,
        komtet_field_exists=field_exists,
        komtet_field_created=field_created,
        komtet_ok=komtet_ok,
        komtet_message=komtet_message,
        komtet_queue_active=queue_active,
        komtet_delivery_service_active=delivery_service_active,
        warnings=warnings,
    )


def _dict_values_list(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        return [v for v in raw.values() if isinstance(v, dict)]
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, dict)]
    return []


@router.get("/dictionaries", response_model=DictionariesResponse)
async def fetch_dictionaries(
    db: AsyncSession = Depends(get_db),
    crm_factory: Callable[[RuntimeConfig], RetailCRMClient] = Depends(get_retailcrm_factory),
    komtet_factory: Callable[[RuntimeConfig], KomtetClient] = Depends(get_komtet_factory),
) -> DictionariesResponse:
    """Fetch live statuses, payment types, couriers, and queues with graceful degradation."""
    cfg = await load_runtime_config(db)
    warnings: list[str] = []

    crm_statuses: list[dict[str, Any]] = []
    crm_payment_types: list[dict[str, Any]] = []
    crm_couriers: list[dict[str, Any]] = []
    crm_sites: list[dict[str, Any]] = []
    crm_delivery_types: list[dict[str, Any]] = []

    if cfg.retailcrm_url and cfg.retailcrm_api_key:
        try:
            async with crm_factory(cfg) as crm:
                statuses_resp = await crm.get_statuses()
                crm_statuses = _dict_values_list(statuses_resp.get("statuses"))

                pay_resp = await crm.get_payment_types()
                crm_payment_types = _dict_values_list(pay_resp.get("paymentTypes"))

                couriers_resp = await crm.get_couriers()
                crm_couriers = _dict_values_list(couriers_resp.get("couriers"))

                sites_resp = await crm.get_sites()
                crm_sites = _dict_values_list(sites_resp.get("sites"))

                try:
                    delivery_resp = await crm.get_delivery_types()
                    crm_delivery_types = [
                        {
                            "code": str(dt.get("code") or "").strip(),
                            "name": str(dt.get("name") or dt.get("code") or "").strip(),
                            "active": bool(dt.get("active", True)),
                        }
                        for dt in _dict_values_list(delivery_resp.get("deliveryTypes"))
                        if str(dt.get("code") or "").strip()
                    ]
                except RetailCRMError as exc:
                    warnings.append(f"RetailCRM типы доставки: {exc}")
        except (RetailCRMError, ValueError) as exc:
            warnings.append(f"RetailCRM: ошибка загрузки справочников — {exc}")
    else:
        warnings.append("RetailCRM не настроен (отсутствует URL или API-ключ).")

    komtet_employees: list[dict[str, Any]] = []
    komtet_couriers: list[dict[str, Any]] = []
    komtet_queue: Optional[dict[str, Any]] = None

    if cfg.komtet_shop_id and cfg.komtet_secret_key:
        try:
            async with komtet_factory(cfg) as komtet:
                if cfg.komtet_queue_id is not None:
                    try:
                        komtet_queue = await komtet.get_queue(cfg.komtet_queue_id)
                    except KomtetError as exc:
                        warnings.append(f"КОМТЕТ очередь {cfg.komtet_queue_id}: {exc}")

                try:
                    emp_resp = await komtet.get_employees(start=0, limit=100)
                    komtet_employees = list(emp_resp.get("account_employees") or [])
                except KomtetError as exc:
                    warnings.append(f"КОМТЕТ сотрудники: {exc}")

                try:
                    cour_resp = await komtet.get_couriers(start=0, limit=100)
                    komtet_couriers = list(cour_resp.get("account_employees") or [])
                except KomtetError as exc:
                    warnings.append(f"КОМТЕТ курьеры: {exc}")

                # Check if courier orders service is active without breaking dictionary response
                try:
                    await komtet.get_orders(start=0, limit=1)
                except KomtetError as exc:
                    if exc.code == "SRV00" or exc.status_code == 403:
                        warnings.append(
                            f"КОМТЕТ Курьер (заказы): услуга не активирована ({exc.code or exc.status_code}) — {exc.description or exc.message}"
                        )
        except ValueError as exc:
            warnings.append(f"КОМТЕТ: {exc}")
    else:
        warnings.append("КОМТЕТ Касса не настроена (отсутствует Shop ID или Secret Key).")

    return DictionariesResponse(
        retailcrm_statuses=crm_statuses,
        retailcrm_payment_types=crm_payment_types,
        retailcrm_couriers=crm_couriers,
        retailcrm_sites=crm_sites,
        retailcrm_delivery_types=crm_delivery_types,
        komtet_employees=komtet_employees,
        komtet_couriers=komtet_couriers,
        komtet_queue=komtet_queue,
        warnings=warnings,
    )


@router.get("/mappings", response_model=MappingsResponse)
async def read_mappings(
    mapping_type: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> MappingsResponse:
    """List persisted RetailCRM <-> KOMTET mappings."""
    rows = await list_mappings(db, mapping_type=mapping_type)
    return MappingsResponse(items=[MappingItemSchema.model_validate(row) for row in rows])


@router.put("/mappings", response_model=MappingsResponse)
async def save_mappings(
    request: Request,
    payload: MappingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> MappingsResponse:
    """Bulk upsert or replace mappings by type and record an audit log entry."""
    client_ip = get_client_ip(request)
    rows = await upsert_mappings(db, payload.items, replace_types=payload.replace_types)
    db.add(
        SyncLog(
            direction="system",
            level="INFO",
            event_type="mappings_updated",
            message="Обновлены соответствия через панель управления",
            payload={
                "replace_types": list(payload.replace_types),
                "count": len(payload.items),
                "client_ip": client_ip,
            },
        )
    )
    await db.commit()
    logger.info(
        "[Admin] PUT /api/v1/mappings ip=%s replace_types=%s count=%d",
        client_ip,
        list(payload.replace_types),
        len(payload.items),
    )
    return MappingsResponse(items=[MappingItemSchema.model_validate(row) for row in rows])


@router.get("/logs", response_model=SyncLogsResponse)
async def read_logs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    level: Optional[str] = Query(default=None),
    direction: Optional[str] = Query(default=None),
    order_id: Optional[int] = Query(default=None),
    event_type: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> SyncLogsResponse:
    """Paginated and filterable audit log of sync events and errors."""
    conditions = []

    _SKIPPED_EVENTS = (
        "sync_skipped",
        "delivery_type_not_allowed",
        "anti_loop_skipped",
        "callback_duplicate_skipped",
        "callback_state_update",
    )

    effective_level = (level or status_filter or "").strip()
    if effective_level and effective_level.lower() != "all":
        normalized_level = effective_level.upper()
        lower_status = effective_level.lower()
        if normalized_level in {"INFO", "WARNING", "ERROR"}:
            conditions.append(SyncLog.level == normalized_level)
        elif lower_status == "skipped":
            conditions.append(SyncLog.event_type.in_(_SKIPPED_EVENTS))
        elif lower_status == "success":
            conditions.append(SyncLog.level != "ERROR")
            conditions.append(SyncLog.event_type.not_in(_SKIPPED_EVENTS))
        else:
            conditions.append(SyncLog.event_type == effective_level)

    if direction:
        dir_clean = direction.strip().lower()
        dir_aliases = {
            "retailcrm_to_komtet": "crm_to_komtet",
            "komtet_to_retailcrm": "komtet_to_crm",
        }
        if dir_clean != "all":
            conditions.append(SyncLog.direction == dir_aliases.get(dir_clean, direction.strip()))
    if event_type:
        conditions.append(SyncLog.event_type == event_type.strip())
    if order_id is not None:
        conditions.append(
            or_(
                SyncLog.retailcrm_order_id == order_id,
                SyncLog.komtet_order_id == order_id,
            )
        )

    count_stmt = select(func.count(SyncLog.id))
    data_stmt = select(SyncLog).order_by(SyncLog.id.desc()).limit(limit).offset(offset)

    if conditions:
        count_stmt = count_stmt.where(*conditions)
        data_stmt = data_stmt.where(*conditions)

    total = int((await db.scalar(count_stmt)) or 0)
    rows = (await db.execute(data_stmt)).scalars().all()

    return SyncLogsResponse(
        items=[SyncLogItemSchema.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/orders-sync", response_model=OrderSyncListResponse)
async def read_orders_sync(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sync_status: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> OrderSyncListResponse:
    """List order synchronization linkage records."""
    count_stmt = select(func.count(OrderSync.id))
    data_stmt = select(OrderSync).order_by(OrderSync.updated_at.desc()).limit(limit).offset(offset)

    if sync_status:
        count_stmt = count_stmt.where(OrderSync.sync_status == sync_status.strip())
        data_stmt = data_stmt.where(OrderSync.sync_status == sync_status.strip())

    total = int((await db.scalar(count_stmt)) or 0)
    rows = (await db.execute(data_stmt)).scalars().all()
    return OrderSyncListResponse(
        items=[OrderSyncItemSchema.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/sync/manual/{order_id}")
async def manual_sync_order(
    request: Request,
    order_id: int,
    ignore_status_filter: bool = Query(default=False),
    db: AsyncSession = Depends(get_db),
    crm_factory: Callable[[RuntimeConfig], RetailCRMClient] = Depends(get_retailcrm_factory),
    komtet_factory: Callable[[RuntimeConfig], KomtetClient] = Depends(get_komtet_factory),
) -> dict[str, Any]:
    """Manually push a RetailCRM order by ID to KOMTET Kassa from the admin panel."""
    if order_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="order_id must be a positive integer",
        )

    started_at = time.perf_counter()
    client_ip = get_client_ip(request)
    logger.info(
        "[Admin] POST /api/v1/sync/manual/%d started ip=%s ignore_status_filter=%s",
        order_id,
        client_ip,
        ignore_status_filter,
    )

    cfg = await load_runtime_config(db)
    try:
        async with crm_factory(cfg) as crm, komtet_factory(cfg) as komtet:
            result = await sync_retailcrm_to_komtet(
                db,
                order_id=order_id,
                trigger_event="manual",
                force=True,
                ignore_status_filter=ignore_status_filter,
                crm_client=crm,
                komtet_client=komtet,
            )
    except ValueError as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.warning(
            "[Admin] POST /api/v1/sync/manual/%d failed ip=%s reason=credentials_not_configured "
            "(%.0f ms): %s",
            order_id,
            client_ip,
            elapsed_ms,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Integration credentials are not configured: {exc}",
        ) from exc

    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    logger.info(
        "[Admin] POST /api/v1/sync/manual/%d done ip=%s status=%s komtet_id=%s reason=%s (%.0f ms)",
        order_id,
        client_ip,
        result.get("status"),
        result.get("komtet_order_id") or "-",
        result.get("reason") or "-",
        elapsed_ms,
    )
    return {"ok": result.get("status") != "error", "result": result}
