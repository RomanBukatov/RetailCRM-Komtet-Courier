"""Bidirectional synchronization engine between RetailCRM and KOMTET Kassa."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.komtet import KomtetClient, KomtetError
from app.clients.retailcrm import RetailCRMClient, RetailCRMError
from app.config import get_app_timezone
from app.models import OrderSync, SyncLog
from app.services.mapper import map_retailcrm_order_to_komtet
from app.services.settings_service import (
    MappingLookup,
    RuntimeConfig,
    load_active_mappings,
    load_runtime_config,
)

logger = logging.getLogger(__name__)
_PENNY = Decimal("0.01")
_ZERO = Decimal("0")

# KOMTET state we record locally when *we* deleted the KOMTET order because the
# RetailCRM order was canceled (distinguishes it from a KOMTET-side "canceled").
_STATE_DELETED_BY_SYNC = "deleted"

# RetailCRM payment statuses that may be settled (switched to "paid") by a courier payment.
# Final statuses such as "paid", "fail", "returned" are never overwritten.
_SETTLEABLE_PAYMENT_STATUSES = frozenset({"", "not-paid", "invoice", "wait-approved", "payment-start"})


# ---------------------------------------------------------------------------
# Per-order locking (in-process, self-evicting)
# ---------------------------------------------------------------------------


@dataclass
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_ORDER_LOCKS: dict[int, _LockEntry] = {}


@asynccontextmanager
async def _order_lock(order_id: int) -> AsyncIterator[None]:
    """Serialize all sync work for one RetailCRM order within this process.

    Entries are reference-counted and evicted as soon as no coroutine holds or
    awaits the lock, so the registry cannot grow unboundedly over the service
    lifetime. NOTE: this is a per-process guard; run a single worker process
    (the SQLite deployment model) or replace with a distributed lock.
    """
    entry = _ORDER_LOCKS.get(order_id)
    if entry is None:
        entry = _LockEntry()
        _ORDER_LOCKS[order_id] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users <= 0 and _ORDER_LOCKS.get(order_id) is entry:
            del _ORDER_LOCKS[order_id]


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _is_transient_error(exc: BaseException) -> bool:
    """Return True when an error is likely transient and safe/desirable to retry."""
    if isinstance(exc, (RetailCRMError, KomtetError)):
        return bool(exc.is_transient)
    if isinstance(exc, (httpx.RequestError, OperationalError, TimeoutError, ConnectionError)):
        return True
    return False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_decimal(value: Any, default: str = "0.00") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid monetary value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Invalid monetary value: {value!r}")
    return result.quantize(_PENNY, rounding=ROUND_HALF_UP)


def _to_quantity(value: Any) -> Decimal:
    if value is None or value == "":
        return _ZERO
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid quantity value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"Invalid quantity value: {value!r}")
    return result


def build_retailcrm_client(cfg: RuntimeConfig) -> RetailCRMClient:
    """Create a RetailCRMClient from resolved runtime settings."""
    return RetailCRMClient(base_url=cfg.retailcrm_url, api_key=cfg.retailcrm_api_key)


def build_komtet_client(cfg: RuntimeConfig) -> KomtetClient:
    """Create a KomtetClient from resolved runtime settings."""
    return KomtetClient(
        shop_id=cfg.komtet_shop_id,
        secret_key=cfg.komtet_secret_key,
        base_url=cfg.komtet_base_url,
        api_version=cfg.komtet_api_version,
    )


async def _add_sync_log(
    session: AsyncSession,
    *,
    direction: str,
    level: str,
    event_type: str,
    message: str,
    retailcrm_order_id: Optional[int] = None,
    komtet_order_id: Optional[int] = None,
    payload: Optional[dict[str, Any]] = None,
    error_details: Optional[str] = None,
) -> None:
    session.add(
        SyncLog(
            direction=direction,
            level=level,
            event_type=event_type,
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            message=message,
            payload=payload,
            error_details=error_details,
        )
    )


async def _record_unexpected_failure(
    session: AsyncSession,
    *,
    direction: str,
    event_type: str,
    retailcrm_order_id: Optional[int],
    komtet_order_id: Optional[int],
    exc: BaseException,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Roll back a possibly broken transaction and persist the failure in a clean one."""
    try:
        await session.rollback()
        if retailcrm_order_id is not None:
            row = await session.scalar(
                select(OrderSync)
                .where(OrderSync.retailcrm_order_id == retailcrm_order_id)
                .execution_options(populate_existing=True)
            )
            if row is not None:
                row.sync_status = "error"
                row.last_error = str(exc)
        await _add_sync_log(
            session,
            direction=direction,
            level="ERROR",
            event_type=event_type,
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            message=f"Unexpected error: {exc}",
            payload=payload,
            error_details=repr(exc),
        )
        await session.commit()
    except Exception:  # pragma: no cover - last-resort guard, never mask the original error
        logger.exception("Failed to persist sync failure for RetailCRM order %s", retailcrm_order_id)
        await session.rollback()


def _extract_komtet_id_from_order(order: dict[str, Any], field_code: str) -> Optional[int]:
    custom_fields = order.get("customFields")
    if not isinstance(custom_fields, dict):
        return None
    raw_val = custom_fields.get(field_code)
    if raw_val in (None, "", 0, "0"):
        return None
    try:
        return int(Decimal(str(raw_val).strip()))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _compute_callback_hash(
    komtet_order_id: Optional[int],
    state: str,
    payment_type: str,
    amount: Any,
    items: list[dict[str, Any]],
) -> str:
    canonical = json.dumps(
        {
            "komtet_order_id": komtet_order_id,
            "state": state,
            "payment_type": payment_type,
            "amount": str(amount) if amount is not None else None,
            "items": items,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_crm_payments(order: dict[str, Any]) -> list[dict[str, Any]]:
    raw = order.get("payments")
    if isinstance(raw, dict):
        return [p for p in raw.values() if isinstance(p, dict)]
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    return []


def _strip_split_suffix(raw_id: Any) -> str:
    text = str(raw_id if raw_id is not None else "").strip()
    if text.endswith("_split"):
        return text[: -len("_split")]
    return text


def _crm_item_name(crm_item: dict[str, Any]) -> str:
    offer = crm_item.get("offer") if isinstance(crm_item.get("offer"), dict) else {}
    return str(
        offer.get("displayName") or offer.get("name") or crm_item.get("productName") or ""
    ).strip().lower()


def _qty_out(qty: Decimal) -> int | float:
    return int(qty) if qty == qty.to_integral_value() else float(qty)


# ---------------------------------------------------------------------------
# Partial redemption & payments
# ---------------------------------------------------------------------------


def _cb_item_total(cb_item: dict[str, Any]) -> Decimal:
    """Extract monetary total for a single KOMTET callback item."""
    if cb_item.get("total") not in (None, ""):
        return _to_decimal(cb_item["total"])
    price = _to_decimal(cb_item.get("price", 0))
    qty = _to_quantity(cb_item.get("quantity", 0))
    return (price * qty).quantize(_PENNY, rounding=ROUND_HALF_UP)


def _reconcile_partial_redemption(
    crm_order: dict[str, Any],
    callback_items: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """Compare KOMTET callback items against RetailCRM order items for partial redemption.

    Matching rules (strict, to never corrupt the RetailCRM order composition):
      1. A callback line is matched strictly by its RetailCRM identifier (`external_id`
         or `order_item_id`, with the `_split` suffix stripped so penny-split rows are
         re-aggregated). KOMTET's internal `cb_item["id"]` is NEVER matched against CRM IDs.
      2. Lines without `external_id`/`order_item_id` fall back to normalized name matching;
         name buckets are *consumed* (capped by the CRM quantity).
      3. Any callback goods line that cannot be matched aborts reconciliation with ValueError.
      4. Full goods refusal (e.g. client paid only delivery or all goods have quantity 0)
         clears goods items in RetailCRM and resets order-level manual discount.
      5. When `order.discountManualAmount > 0` and items change, order-level discount is
         recalculated so RetailCRM's resulting order total matches KOMTET's fiscalized total.
    """
    if not callback_items:
        return False, {}

    crm_items = [i for i in (crm_order.get("items") or []) if isinstance(i, dict)]
    if not crm_items:
        return False, {}

    crm_ids = {str(i["id"]) for i in crm_items if i.get("id") is not None}
    qty_by_id: dict[str, Decimal] = {}
    qty_by_name: dict[str, Decimal] = {}
    goods_lines = 0
    has_zero_goods_line = False
    goods_sum_from_cb = Decimal("0.00")
    has_delivery_in_cb = False
    cb_delivery_total = Decimal("0.00")

    for cb_item in callback_items:
        if not isinstance(cb_item, dict):
            continue
        item_type = str(cb_item.get("type") or "").strip().lower()
        name = str(cb_item.get("name") or "").strip()
        # Only use RetailCRM-originated external identifiers; NEVER fall back to KOMTET's
        # internal `cb_item["id"]`, which could collide with unrelated CRM item IDs.
        raw_ext = cb_item.get("external_id")
        if raw_ext in (None, ""):
            raw_ext = cb_item.get("order_item_id")
        base_id = _strip_split_suffix(raw_ext) if raw_ext not in (None, "") else ""

        if base_id == "delivery" or (item_type == "service" and "доставка" in name.lower()):
            if _to_quantity(cb_item.get("quantity", 1)) > _ZERO:
                has_delivery_in_cb = True
                cb_delivery_total += _cb_item_total(cb_item)
            continue

        qty = _to_quantity(cb_item.get("quantity", 0))
        if qty <= _ZERO:
            has_zero_goods_line = True
            continue

        goods_lines += 1
        goods_sum_from_cb += _cb_item_total(cb_item)

        if base_id and base_id in crm_ids:
            qty_by_id[base_id] = qty_by_id.get(base_id, _ZERO) + qty
        elif name:
            norm_name = name.lower()
            qty_by_name[norm_name] = qty_by_name.get(norm_name, _ZERO) + qty
        else:
            raise ValueError(
                f"KOMTET callback item {raw_ext!r} has neither a known external_id nor a name; "
                "refusing to modify RetailCRM order composition"
            )

    delivery = crm_order.get("delivery") if isinstance(crm_order.get("delivery"), dict) else {}
    crm_delivery_cost = _to_decimal(delivery.get("cost", 0))
    crm_order_discount_amt = _to_decimal(crm_order.get("discountManualAmount", 0))
    crm_order_discount_pct = _to_decimal(crm_order.get("discountManualPercent", 0))

    def _build_delivery_patch(new_cost: Decimal) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "cost": 0 if new_cost == Decimal("0.00") else float(new_cost),
        }
        if delivery.get("code"):
            patch["code"] = delivery["code"]
        return patch

    if goods_lines == 0:
        if not has_delivery_in_cb and not has_zero_goods_line:
            return False, {}
        # Full refusal of all goods (client paid only delivery, or all goods qty == 0)
        edit_payload: dict[str, Any] = {"items": []}
        if crm_order_discount_amt > Decimal("0.00"):
            edit_payload["discountManualAmount"] = 0
        if crm_order_discount_pct > Decimal("0.00"):
            edit_payload["discountManualPercent"] = 0
        if has_delivery_in_cb and cb_delivery_total != crm_delivery_cost:
            edit_payload["delivery"] = _build_delivery_patch(cb_delivery_total)
        elif not has_delivery_in_cb and crm_delivery_cost > Decimal("0.00"):
            edit_payload["delivery"] = _build_delivery_patch(Decimal("0.00"))
        return True, edit_payload

    updated_items: list[dict[str, Any]] = []
    items_changed = False
    remaining_before_order_discount = Decimal("0.00")

    for crm_item in crm_items:
        crm_id = str(crm_item.get("id")) if crm_item.get("id") is not None else ""
        offer = crm_item.get("offer") if isinstance(crm_item.get("offer"), dict) else {}
        crm_name = _crm_item_name(crm_item)
        old_qty = _to_quantity(crm_item.get("quantity", 0))

        if crm_id and crm_id in qty_by_id:
            new_qty = qty_by_id[crm_id]
        elif crm_name and qty_by_name.get(crm_name, _ZERO) > _ZERO:
            new_qty = min(qty_by_name[crm_name], old_qty)
            qty_by_name[crm_name] -= new_qty
        else:
            new_qty = _ZERO

        if new_qty != old_qty:
            items_changed = True

        if new_qty > _ZERO:
            item_patch: dict[str, Any] = {"quantity": _qty_out(new_qty)}
            if crm_item.get("id") is not None:
                item_patch = {"id": crm_item["id"], **item_patch}
            if offer.get("id") is not None:
                item_patch["offer"] = {"id": offer["id"]}
            elif offer.get("externalId") is not None:
                item_patch["offer"] = {"externalId": offer["externalId"]}
            if crm_item.get("productName") is not None:
                item_patch["productName"] = crm_item["productName"]
            if crm_item.get("initialPrice") is not None:
                item_patch["initialPrice"] = crm_item["initialPrice"]
            if crm_item.get("purchasePrice") is not None:
                item_patch["purchasePrice"] = crm_item["purchasePrice"]
            if crm_item.get("vatRate") is not None:
                item_patch["vatRate"] = crm_item["vatRate"]
            if crm_item.get("properties"):
                item_patch["properties"] = crm_item["properties"]
            if crm_item.get("discountManualAmount") is not None:
                item_patch["discountManualAmount"] = crm_item["discountManualAmount"]
            if crm_item.get("discountManualPercent") is not None:
                item_patch["discountManualPercent"] = crm_item["discountManualPercent"]
            updated_items.append(item_patch)

            init_price = _to_decimal(crm_item.get("initialPrice", crm_item.get("price", 0)))
            item_disc_amt = _to_decimal(crm_item.get("discountManualAmount", 0))
            item_disc_pct = _to_decimal(crm_item.get("discountManualPercent", 0))
            item_unit_net = max(
                Decimal("0.00"),
                init_price - item_disc_amt - (init_price * item_disc_pct / Decimal("100")),
            )
            remaining_before_order_discount += (item_unit_net * new_qty).quantize(
                _PENNY, rounding=ROUND_HALF_UP
            )

    leftovers = sorted(name for name, qty in qty_by_name.items() if qty > _ZERO)
    if leftovers:
        raise ValueError(
            "KOMTET callback contains goods that cannot be matched to RetailCRM order items: "
            + ", ".join(leftovers)
        )

    edit_payload = {}
    if items_changed:
        edit_payload["items"] = updated_items
        # Recalculate order-level manual discount so RetailCRM total matches KOMTET fiscalized receipt.
        if crm_order_discount_amt > Decimal("0.00") or crm_order_discount_pct > Decimal("0.00"):
            needed_discount = max(
                Decimal("0.00"),
                (remaining_before_order_discount - goods_sum_from_cb).quantize(
                    _PENNY, rounding=ROUND_HALF_UP
                ),
            )
            edit_payload["discountManualAmount"] = float(needed_discount)
            if crm_order_discount_pct > Decimal("0.00"):
                edit_payload["discountManualPercent"] = 0

    # Reconcile delivery cost while preserving existing `delivery.code`.
    if crm_delivery_cost > Decimal("0.00") and not has_delivery_in_cb and goods_sum_from_cb > Decimal("0.00"):
        edit_payload["delivery"] = _build_delivery_patch(Decimal("0.00"))
        items_changed = True
    elif has_delivery_in_cb and cb_delivery_total != crm_delivery_cost:
        edit_payload["delivery"] = _build_delivery_patch(cb_delivery_total)
        items_changed = True

    return items_changed, edit_payload


def _resolve_crm_payment_type(
    komtet_payment_type: str,
    mappings: MappingLookup,
) -> str:
    if komtet_payment_type and komtet_payment_type in mappings.payments_komtet_to_crm:
        return mappings.payments_komtet_to_crm[komtet_payment_type]
    defaults = {
        "card": "bank-card",
        "cash": "cash",
        "prepayment": "oplachen",
    }
    return defaults.get(komtet_payment_type, "bank-card")


def _pick_payment_to_settle(
    payments: list[dict[str, Any]],
    target_type: str,
) -> Optional[dict[str, Any]]:
    """Pick an existing unsettled payment to mark as paid instead of creating a new one.

    Prefer a payment of the same type; otherwise reuse the first unsettled payment of any
    type (switching its type). Creating an additional payment next to an unsettled one
    would make the sum of order payments exceed the order total in RetailCRM.
    """
    candidates = [
        p
        for p in payments
        if p.get("id") is not None
        and str(p.get("status") or "").strip() in _SETTLEABLE_PAYMENT_STATUSES
    ]
    same_type = next((p for p in candidates if p.get("type") == target_type), None)
    if same_type is not None:
        return same_type
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# RetailCRM -> KOMTET
# ---------------------------------------------------------------------------


async def sync_retailcrm_to_komtet(
    session: AsyncSession,
    order_id: int,
    trigger_event: str = "webhook",
    *,
    site: Optional[str] = None,
    force: bool = False,
    ignore_status_filter: bool = False,
    crm_client: Optional[RetailCRMClient] = None,
    komtet_client: Optional[KomtetClient] = None,
) -> dict[str, Any]:
    """Synchronize a single RetailCRM order to KOMTET Kassa (create, update, or cancel)."""
    async with _order_lock(order_id):
        return await _sync_retailcrm_to_komtet_locked(
            session,
            order_id,
            trigger_event=trigger_event,
            site=site,
            force=force,
            ignore_status_filter=ignore_status_filter,
            crm_client=crm_client,
            komtet_client=komtet_client,
        )


async def _resolve_komtet_link(
    session: AsyncSession,
    *,
    order_id: int,
    order_sync: OrderSync,
    crm_komtet_id: Optional[int],
    komtet: KomtetClient,
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Return `(komtet_order_id, note, remote_state)`; `None` id means a new order must be created.

    The local `order_sync` link is authoritative. A `customFields.komtet_id` value without a
    local link (lost DB, restored backup, or an order *copied* in RetailCRM, which copies
    custom fields) is adopted only after verifying it is not linked to another CRM order and
    that the KOMTET order explicitly belongs to this RetailCRM order (`external_id`/`order_id`
    is present and equals `str(order_id)`).
    """
    if order_sync.komtet_order_id is not None:
        if order_sync.sync_status == "canceled":
            return (
                None,
                (
                    f"Previous KOMTET order #{order_sync.komtet_order_id} was canceled/deleted; "
                    "a new KOMTET order will be created"
                ),
                order_sync.komtet_state,
            )
        return order_sync.komtet_order_id, None, order_sync.komtet_state

    if crm_komtet_id is None:
        return None, None, None

    owner_row = await session.scalar(
        select(OrderSync).where(
            OrderSync.komtet_order_id == crm_komtet_id,
            OrderSync.retailcrm_order_id != order_id,
        )
    )
    if owner_row is not None:
        return (
            None,
            (
                f"customFields komtet_id={crm_komtet_id} is already linked to RetailCRM order "
                f"#{owner_row.retailcrm_order_id} (copied order?); ignoring it"
            ),
            None,
        )

    try:
        info = await komtet.get_order(crm_komtet_id)
    except KomtetError as exc:
        if exc.status_code == 404:
            return (
                None,
                f"KOMTET order #{crm_komtet_id} from customFields does not exist; ignoring it",
                None,
            )
        raise

    owner = info.get("external_id")
    if owner in (None, ""):
        owner = info.get("order_id")
    if owner in (None, "") or str(owner).strip() != str(order_id):
        return (
            None,
            (
                f"KOMTET order #{crm_komtet_id} has missing or mismatched external_id={owner!r}, "
                f"expected RetailCRM order #{order_id}; ignoring it"
            ),
            None,
        )

    remote_state = str(info.get("state") or "").strip().lower() or None
    return (
        crm_komtet_id,
        f"Recovered link to KOMTET order #{crm_komtet_id} from RetailCRM customFields",
        remote_state,
    )


async def _write_back_komtet_id(
    session: AsyncSession,
    *,
    crm: RetailCRMClient,
    cfg: RuntimeConfig,
    order_id: int,
    site: Optional[str],
    komtet_id: int,
    order_sync: OrderSync,
) -> Optional[str]:
    """Best-effort save of `customFields.komtet_id` to RetailCRM; returns warning text on failure."""
    try:
        await crm.edit_order(
            order_id,
            {"customFields": {cfg.komtet_custom_field_code: komtet_id}},
            by="id",
            site=site,
        )
        return None
    except RetailCRMError as exc:
        message = (
            f"KOMTET order #{komtet_id} is linked locally, but saving "
            f"customFields.{cfg.komtet_custom_field_code} to RetailCRM failed: {exc}"
        )
        logger.warning(message)
        order_sync.last_error = message
        await _add_sync_log(
            session,
            direction="crm_to_komtet",
            level="WARNING",
            event_type="crm_writeback_failed",
            retailcrm_order_id=order_id,
            komtet_order_id=komtet_id,
            message=message,
            error_details=repr(exc),
        )
        await session.commit()
        return message


async def _sync_retailcrm_to_komtet_locked(
    session: AsyncSession,
    order_id: int,
    *,
    trigger_event: str = "webhook",
    site: Optional[str] = None,
    force: bool = False,
    ignore_status_filter: bool = False,
    crm_client: Optional[RetailCRMClient] = None,
    komtet_client: Optional[KomtetClient] = None,
) -> dict[str, Any]:
    cfg = await load_runtime_config(session)
    mappings = await load_active_mappings(session)

    if not cfg.auto_sync_enabled and not force:
        await _add_sync_log(
            session,
            direction="crm_to_komtet",
            level="INFO",
            event_type="sync_skipped",
            retailcrm_order_id=order_id,
            message="Auto sync is disabled in settings",
            payload={"trigger_event": trigger_event},
        )
        await session.commit()
        return {"status": "skipped", "reason": "auto_sync_disabled", "retailcrm_order_id": order_id}

    order_sync = await session.scalar(
        select(OrderSync)
        .where(OrderSync.retailcrm_order_id == order_id)
        .execution_options(populate_existing=True)
    )

    now = _utc_now()
    if not force and order_sync is not None:
        last_cb = _ensure_utc(order_sync.last_komtet_callback_at)
        if last_cb is not None:
            elapsed = (now - last_cb).total_seconds()
            if elapsed < cfg.anti_loop_seconds:
                await _add_sync_log(
                    session,
                    direction="crm_to_komtet",
                    level="INFO",
                    event_type="anti_loop_skipped",
                    retailcrm_order_id=order_id,
                    komtet_order_id=order_sync.komtet_order_id,
                    message=(
                        f"Skipped webhook within {cfg.anti_loop_seconds}s anti-loop window "
                        f"({elapsed:.1f}s since KOMTET callback)"
                    ),
                    payload={"trigger_event": trigger_event},
                )
                await session.commit()
                return {
                    "status": "skipped",
                    "reason": "anti_loop",
                    "retailcrm_order_id": order_id,
                }

    owns_crm = crm_client is None
    owns_komtet = komtet_client is None
    crm = crm_client
    komtet = komtet_client
    komtet_id: Optional[int] = order_sync.komtet_order_id if order_sync is not None else None

    try:
        if crm is None:
            crm = build_retailcrm_client(cfg)
        lookup_site = (
            (order_sync.retailcrm_site if order_sync is not None else None)
            or (site.strip() if site else None)
            or (cfg.retailcrm_default_site or None)
        )
        crm_resp = await crm.get_order(
            order_id,
            by="id",
            site=lookup_site,
        )
        order = crm_resp.get("order") or {}
        if not order:
            raise RetailCRMError(f"RetailCRM order {order_id} payload is empty")

        status = str(order.get("status") or "").strip()
        resolved_site = (
            order.get("site")
            or (order_sync.retailcrm_site if order_sync is not None else None)
            or (site.strip() if site else None)
            or (cfg.retailcrm_default_site or None)
        )
        order_number = str(order.get("number") or "")
        crm_komtet_id = _extract_komtet_id_from_order(order, cfg.komtet_custom_field_code)

        if order_sync is None:
            order_sync = OrderSync(
                retailcrm_order_id=order_id,
                retailcrm_order_number=order_number or None,
                retailcrm_site=resolved_site,
                komtet_external_id=str(order_id),
                sync_status="pending",
            )
            session.add(order_sync)
        else:
            order_sync.retailcrm_order_number = order_number or order_sync.retailcrm_order_number
            order_sync.retailcrm_site = resolved_site or order_sync.retailcrm_site

        # Guard: an order already fiscalized (`komtet_state == "done"`) must NEVER be
        # deleted or overwritten in KOMTET, even if its CRM status is later moved to cancel.
        if not force and order_sync.komtet_state == "done":
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="anti_loop_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=order_sync.komtet_order_id,
                message=(
                    f"Skipped order already fiscalized in KOMTET (state='done', "
                    f"crm_status='{status}')"
                ),
                payload={"trigger_event": trigger_event, "crm_status": status},
            )
            await session.commit()
            return {
                "status": "skipped",
                "reason": "already_done",
                "retailcrm_order_id": order_id,
                "komtet_order_id": order_sync.komtet_order_id,
            }

        # Handle cancellation statuses
        if status in cfg.cancel_statuses:
            if komtet is None:
                komtet = build_komtet_client(cfg)
            komtet_id, link_note, remote_state = await _resolve_komtet_link(
                session,
                order_id=order_id,
                order_sync=order_sync,
                crm_komtet_id=crm_komtet_id,
                komtet=komtet,
            )
            if komtet_id is not None and remote_state == "done":
                order_sync.komtet_order_id = komtet_id
                order_sync.komtet_state = "done"
                order_sync.sync_status = "completed"
                order_sync.last_error = None
                await _add_sync_log(
                    session,
                    direction="crm_to_komtet",
                    level="INFO",
                    event_type="anti_loop_skipped",
                    retailcrm_order_id=order_id,
                    komtet_order_id=komtet_id,
                    message=(
                        f"Recovered KOMTET order #{komtet_id} is already 'done'; "
                        f"refusing to delete on CRM status '{status}'"
                    ),
                    payload={"trigger_event": trigger_event, "crm_status": status},
                )
                await session.commit()
                return {
                    "status": "skipped",
                    "reason": "already_done",
                    "retailcrm_order_id": order_id,
                    "komtet_order_id": komtet_id,
                }
            if komtet_id is not None:
                try:
                    await komtet.delete_order(komtet_id)
                except KomtetError as exc:
                    if exc.status_code != 404:
                        raise

                order_sync.komtet_order_id = komtet_id
                order_sync.komtet_state = _STATE_DELETED_BY_SYNC
                order_sync.sync_status = "canceled"
                order_sync.last_hash = None
                order_sync.last_synced_at = now
                order_sync.last_error = None
                await _add_sync_log(
                    session,
                    direction="crm_to_komtet",
                    level="INFO",
                    event_type="order_cancel",
                    retailcrm_order_id=order_id,
                    komtet_order_id=komtet_id,
                    message=f"Canceled/deleted order in KOMTET due to CRM status '{status}'",
                    payload={"trigger_event": trigger_event, "crm_status": status},
                )
                await session.commit()
                return {
                    "status": "canceled",
                    "retailcrm_order_id": order_id,
                    "komtet_order_id": komtet_id,
                }

            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="sync_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=order_sync.komtet_order_id,
                message=(
                    f"Canceled order '{status}' does not require KOMTET action"
                    + (f" ({link_note})" if link_note else "")
                ),
                payload={"trigger_event": trigger_event, "crm_status": status},
            )
            await session.commit()
            return {
                "status": "skipped",
                "reason": "canceled_no_action",
                "retailcrm_order_id": order_id,
            }

        # Anti-loop guard for already completed final status
        if not force and (
            status == cfg.final_status
            or (order_sync.sync_status == "completed" and order_sync.komtet_state == "done")
        ):
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="anti_loop_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id,
                message=f"Skipped order in final status '{status}'",
                payload={"trigger_event": trigger_event, "crm_status": status},
            )
            await session.commit()
            return {
                "status": "skipped",
                "reason": "final_status",
                "retailcrm_order_id": order_id,
            }

        # Check trigger status allowlist
        if not ignore_status_filter and cfg.trigger_statuses and status not in cfg.trigger_statuses:
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="sync_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id,
                message=f"CRM status '{status}' is not in trigger_statuses",
                payload={"trigger_event": trigger_event, "crm_status": status},
            )
            await session.commit()
            return {
                "status": "skipped",
                "reason": "status_not_triggered",
                "retailcrm_order_id": order_id,
                "crm_status": status,
            }

        # Order canceled on the KOMTET side (e.g. by courier): never silently re-create it.
        if (
            not force
            and order_sync.komtet_order_id is not None
            and order_sync.sync_status == "canceled"
            and order_sync.komtet_state != _STATE_DELETED_BY_SYNC
        ):
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="WARNING",
                event_type="sync_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=order_sync.komtet_order_id,
                message=(
                    f"KOMTET order #{order_sync.komtet_order_id} was canceled in KOMTET; "
                    "use manual sync to create a new delivery order"
                ),
                payload={"trigger_event": trigger_event, "crm_status": status},
            )
            await session.commit()
            return {
                "status": "skipped",
                "reason": "komtet_canceled",
                "retailcrm_order_id": order_id,
                "komtet_order_id": order_sync.komtet_order_id,
            }

        komtet_payload, payload_hash = map_retailcrm_order_to_komtet(order, cfg, mappings)

        if komtet is None:
            komtet = build_komtet_client(cfg)

        komtet_id, link_note, remote_state = await _resolve_komtet_link(
            session,
            order_id=order_id,
            order_sync=order_sync,
            crm_komtet_id=crm_komtet_id,
            komtet=komtet,
        )
        if link_note:
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="WARNING" if komtet_id is None else "INFO",
                event_type="komtet_link_resolved",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id or crm_komtet_id,
                message=link_note,
                payload={"trigger_event": trigger_event},
            )
            if komtet_id is not None:
                order_sync.komtet_order_id = komtet_id
                await session.flush()

        # If the recovered KOMTET order is already fiscalized (`state == "done"`),
        # mark the local record as completed and never issue a PUT update_order.
        if komtet_id is not None and remote_state == "done":
            order_sync.komtet_order_id = komtet_id
            order_sync.komtet_state = "done"
            order_sync.sync_status = "completed"
            order_sync.last_error = None
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="anti_loop_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id,
                message=(
                    f"KOMTET order #{komtet_id} is already 'done'; "
                    "marked local sync as completed without updating KOMTET"
                ),
                payload={"trigger_event": trigger_event, "crm_status": status},
            )
            await session.commit()
            return {
                "status": "skipped",
                "reason": "already_done",
                "retailcrm_order_id": order_id,
                "komtet_order_id": komtet_id,
            }

        if komtet_id is not None and remote_state == "canceled" and not force:
            order_sync.komtet_order_id = komtet_id
            order_sync.komtet_state = "canceled"
            order_sync.sync_status = "canceled"
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="WARNING",
                event_type="sync_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id,
                message=(
                    f"Recovered KOMTET order #{komtet_id} is canceled in KOMTET; "
                    "use manual sync to create a new delivery order"
                ),
                payload={"trigger_event": trigger_event, "crm_status": status},
            )
            await session.commit()
            return {
                "status": "skipped",
                "reason": "komtet_canceled",
                "retailcrm_order_id": order_id,
                "komtet_order_id": komtet_id,
            }
        if komtet_id is not None and remote_state == "canceled" and force:
            komtet_id = None

        async def _create_in_komtet(reason: str) -> dict[str, Any]:
            created = await komtet.create_order(komtet_payload)
            raw_new_id = created.get("id")
            try:
                new_komtet_id = int(str(raw_new_id).strip())
            except (TypeError, ValueError) as exc:
                raise KomtetError(
                    f"KOMTET create_order response has invalid 'id': {raw_new_id!r}",
                    response_data=created,
                ) from exc

            # Persist the link immediately: if anything below fails, the next sync must
            # update this KOMTET order instead of creating a duplicate one.
            order_sync.komtet_order_id = new_komtet_id
            order_sync.komtet_external_id = str(order_id)
            order_sync.komtet_state = str(created.get("state") or "new")
            order_sync.sync_status = "synced"
            order_sync.last_hash = payload_hash
            order_sync.last_synced_at = now
            order_sync.last_error = None
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="order_create",
                retailcrm_order_id=order_id,
                komtet_order_id=new_komtet_id,
                message=f"Created order #{new_komtet_id} in KOMTET for RetailCRM #{order_id} ({reason})",
                payload={"trigger_event": trigger_event, "komtet_payload": komtet_payload},
            )
            await session.commit()

            warning = await _write_back_komtet_id(
                session,
                crm=crm,
                cfg=cfg,
                order_id=order_id,
                site=resolved_site,
                komtet_id=new_komtet_id,
                order_sync=order_sync,
            )
            result: dict[str, Any] = {
                "status": "created",
                "retailcrm_order_id": order_id,
                "komtet_order_id": new_komtet_id,
            }
            if warning:
                result["warning"] = warning
            return result

        if komtet_id is None:
            return await _create_in_komtet("new order" if not link_note else "stale link")

        # Existing KOMTET order -> check hash idempotency unless forced
        if not force and order_sync.last_hash == payload_hash:
            warning = None
            if crm_komtet_id != komtet_id:
                warning = await _write_back_komtet_id(
                    session, crm=crm, cfg=cfg, order_id=order_id, site=resolved_site,
                    komtet_id=komtet_id, order_sync=order_sync,
                )
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="sync_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id,
                message="Order payload hash unchanged; skipped KOMTET update",
                payload={"trigger_event": trigger_event},
            )
            await session.commit()
            result = {
                "status": "skipped",
                "reason": "unchanged",
                "retailcrm_order_id": order_id,
                "komtet_order_id": komtet_id,
            }
            if warning:
                result["warning"] = warning
            return result

        # Never send `state` on update: it would reset the courier workflow state in KOMTET.
        update_payload = {k: v for k, v in komtet_payload.items() if k != "state"}
        try:
            updated = await komtet.update_order(komtet_id, update_payload)
        except KomtetError as exc:
            if exc.status_code != 404:
                raise
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="WARNING",
                event_type="komtet_order_missing",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id,
                message=f"KOMTET order #{komtet_id} not found on update; creating a new one",
                payload={"trigger_event": trigger_event},
            )
            return await _create_in_komtet(f"KOMTET order #{komtet_id} was missing")

        order_sync.komtet_order_id = komtet_id
        order_sync.komtet_external_id = str(order_id)
        order_sync.komtet_state = str(updated.get("state") or order_sync.komtet_state or "new")
        order_sync.sync_status = "synced"
        order_sync.last_hash = payload_hash
        order_sync.last_synced_at = now
        order_sync.last_error = None

        await _add_sync_log(
            session,
            direction="crm_to_komtet",
            level="INFO",
            event_type="order_update",
            retailcrm_order_id=order_id,
            komtet_order_id=komtet_id,
            message=f"Updated KOMTET order #{komtet_id} from RetailCRM #{order_id}",
            payload={"trigger_event": trigger_event, "komtet_payload": update_payload},
        )
        await session.commit()

        warning = None
        if crm_komtet_id != komtet_id:
            warning = await _write_back_komtet_id(
                session, crm=crm, cfg=cfg, order_id=order_id, site=resolved_site,
                komtet_id=komtet_id, order_sync=order_sync,
            )
        result = {
            "status": "updated",
            "retailcrm_order_id": order_id,
            "komtet_order_id": komtet_id,
        }
        if warning:
            result["warning"] = warning
        return result

    except (KomtetError, RetailCRMError, ValueError) as exc:
        transient = _is_transient_error(exc)
        logger.warning(
            "Sync crm_to_komtet failed for order %s (transient=%s): %s",
            order_id,
            transient,
            exc,
        )
        await session.rollback()
        err_row = await session.scalar(
            select(OrderSync)
            .where(OrderSync.retailcrm_order_id == order_id)
            .execution_options(populate_existing=True)
        )
        if err_row is None:
            err_row = OrderSync(
                retailcrm_order_id=order_id,
                sync_status="error",
                last_error=str(exc),
            )
            session.add(err_row)
        else:
            err_row.sync_status = "error"
            err_row.last_error = str(exc)

        await _add_sync_log(
            session,
            direction="crm_to_komtet",
            level="ERROR",
            event_type="sync_error",
            retailcrm_order_id=order_id,
            komtet_order_id=err_row.komtet_order_id,
            message=f"Failed to sync RetailCRM order #{order_id} to KOMTET: {exc}",
            payload={"trigger_event": trigger_event, "transient_error": transient},
            error_details=repr(exc),
        )
        await session.commit()
        return {
            "status": "error",
            "transient_error": transient,
            "retailcrm_order_id": order_id,
            "error": str(exc),
        }
    except Exception as exc:
        transient = _is_transient_error(exc)
        logger.exception("Unexpected error syncing RetailCRM order %s to KOMTET", order_id)
        await _record_unexpected_failure(
            session,
            direction="crm_to_komtet",
            event_type="sync_error",
            retailcrm_order_id=order_id,
            komtet_order_id=komtet_id,
            exc=exc,
            payload={"trigger_event": trigger_event, "transient_error": transient},
        )
        return {
            "status": "error",
            "transient_error": transient,
            "retailcrm_order_id": order_id,
            "error": str(exc),
        }
    finally:
        if owns_crm and crm is not None:
            await crm.close()
        if owns_komtet and komtet is not None:
            await komtet.close()


# ---------------------------------------------------------------------------
# KOMTET -> RetailCRM
# ---------------------------------------------------------------------------


async def verify_komtet_callback_signature(
    session: AsyncSession,
    cfg: RuntimeConfig,
    payload: dict[str, Any],
    *,
    signature: str = "",
    raw_url: str = "",
    candidate_urls: Optional[list[str]] = None,
    raw_body: bytes = b"",
    query_string: str = "",
    require_signature: bool = False,
) -> None:
    """Verify KOMTET `X-HMAC-Signature` before creating downstream API clients.

    Logs invalid signatures into `sync_logs` and raises `PermissionError`.
    """
    if not (cfg.verify_komtet_signature or require_signature):
        return

    body_text = raw_body.decode("utf-8") if raw_body else ""
    urls_to_check: list[str] = []

    base_candidates: list[str] = []
    if cfg.komtet_callback_url:
        if query_string and "?" not in cfg.komtet_callback_url:
            base_candidates.append(f"{cfg.komtet_callback_url}?{query_string}")
        base_candidates.append(cfg.komtet_callback_url)
    base_candidates.append(raw_url)
    base_candidates.extend(candidate_urls or [])

    for url in base_candidates:
        if url and url not in urls_to_check:
            urls_to_check.append(url)

    is_valid = bool(cfg.komtet_secret_key) and any(
        KomtetClient.verify_signature(
            cfg.komtet_secret_key, "POST", url, body_text, signature
        )
        for url in urls_to_check
    )
    if not is_valid:
        await _add_sync_log(
            session,
            direction="komtet_to_crm",
            level="ERROR",
            event_type="signature_invalid",
            message="Rejected KOMTET callback due to invalid X-HMAC-Signature",
            payload={
                "komtet_id": payload.get("id"),
                "external_id": payload.get("external_id"),
                "checked_urls": urls_to_check,
            },
        )
        await session.commit()
        raise PermissionError("Invalid X-HMAC-Signature")


async def sync_komtet_to_retailcrm(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    signature: str = "",
    raw_url: str = "",
    candidate_urls: Optional[list[str]] = None,
    raw_body: bytes = b"",
    query_string: str = "",
    crm_client: Optional[RetailCRMClient] = None,
    require_signature: bool = False,
    signature_already_verified: bool = False,
) -> dict[str, Any]:
    """Process KOMTET Kassa order callback: verify HMAC, reconcile items, record payment, finish order.

    `require_signature=True` enforces HMAC verification regardless of the DB toggle
    (used in production).
    """
    cfg = await load_runtime_config(session)
    mappings = await load_active_mappings(session)

    if not signature_already_verified:
        await verify_komtet_callback_signature(
            session,
            cfg,
            payload,
            signature=signature,
            raw_url=raw_url,
            candidate_urls=candidate_urls,
            raw_body=raw_body,
            query_string=query_string,
            require_signature=require_signature,
        )

    raw_komtet_id = payload.get("id")
    komtet_order_id: Optional[int] = None
    if raw_komtet_id is not None:
        try:
            komtet_order_id = int(raw_komtet_id)
        except (TypeError, ValueError):
            komtet_order_id = None

    raw_ext_id = payload.get("external_id") or payload.get("order_id")
    state = str(payload.get("state") or "").strip().lower()
    callback_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    callback_payment_type = str(payload.get("payment_type") or "").strip().lower()
    callback_amount = payload.get("amount")

    owns_crm = crm_client is None
    crm = crm_client
    order_sync: Optional[OrderSync] = None
    retailcrm_order_id: Optional[int] = None

    try:
        # Find linked OrderSync row
        if komtet_order_id is not None:
            order_sync = await session.scalar(
                select(OrderSync).where(OrderSync.komtet_order_id == komtet_order_id)
            )
        if order_sync is None and raw_ext_id is not None:
            order_sync = await session.scalar(
                select(OrderSync).where(OrderSync.komtet_external_id == str(raw_ext_id))
            )
            if order_sync is None:
                try:
                    ext_int = int(raw_ext_id)
                    order_sync = await session.scalar(
                        select(OrderSync).where(OrderSync.retailcrm_order_id == ext_int)
                    )
                except (TypeError, ValueError):
                    pass

        retailcrm_order_id = order_sync.retailcrm_order_id if order_sync is not None else None
        if retailcrm_order_id is None and raw_ext_id is not None:
            try:
                retailcrm_order_id = int(raw_ext_id)
            except (TypeError, ValueError):
                retailcrm_order_id = None

        if crm is None:
            crm = build_retailcrm_client(cfg)

        if retailcrm_order_id is None and komtet_order_id is not None:
            order_filters: dict[str, Any] = {
                "customFields": {cfg.komtet_custom_field_code: komtet_order_id}
            }
            if cfg.retailcrm_default_site:
                order_filters["sites"] = [cfg.retailcrm_default_site]
            search_resp = await crm.get_orders(
                filters=order_filters,
                limit=20,
            )
            found_orders = search_resp.get("orders") or []
            if len(found_orders) > 1:
                raise ValueError(
                    f"Ambiguous KOMTET callback: {len(found_orders)} RetailCRM orders have "
                    f"{cfg.komtet_custom_field_code}={komtet_order_id}"
                )
            if found_orders:
                retailcrm_order_id = int(found_orders[0]["id"])

        if retailcrm_order_id is None:
            await _add_sync_log(
                session,
                direction="komtet_to_crm",
                level="ERROR",
                event_type="callback_order_not_found",
                komtet_order_id=komtet_order_id,
                message="Unable to resolve RetailCRM order ID from KOMTET callback",
                payload=payload,
            )
            await session.commit()
            return {"status": "error", "reason": "order_not_found"}

        # Serialize with other callbacks (KOMTET retries) and with RetailCRM->KOMTET sync
        # for the same order: prevents duplicate payments in RetailCRM.
        async with _order_lock(retailcrm_order_id):
            order_sync = await session.scalar(
                select(OrderSync)
                .where(OrderSync.retailcrm_order_id == retailcrm_order_id)
                .execution_options(populate_existing=True)
            )
            return await _process_komtet_callback_locked(
                session,
                cfg=cfg,
                mappings=mappings,
                crm=crm,
                payload=payload,
                order_sync=order_sync,
                retailcrm_order_id=retailcrm_order_id,
                komtet_order_id=komtet_order_id,
                raw_ext_id=raw_ext_id,
                state=state,
                callback_items=callback_items,
                callback_payment_type=callback_payment_type,
                callback_amount=callback_amount,
            )

    except (RetailCRMError, KomtetError, ValueError) as exc:
        transient = _is_transient_error(exc)
        logger.warning("Failed processing KOMTET callback (transient=%s): %s", transient, exc)
        await session.rollback()
        err_row: Optional[OrderSync] = None
        if retailcrm_order_id is not None:
            err_row = await session.scalar(
                select(OrderSync)
                .where(OrderSync.retailcrm_order_id == retailcrm_order_id)
                .execution_options(populate_existing=True)
            )
        if err_row is not None:
            err_row.sync_status = "error"
            err_row.last_error = str(exc)
        await _add_sync_log(
            session,
            direction="komtet_to_crm",
            level="ERROR",
            event_type="callback_error",
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            message=f"Error processing KOMTET callback: {exc}",
            payload={**payload, "_transient_error": transient},
            error_details=repr(exc),
        )
        await session.commit()
        return {
            "status": "error",
            "transient_error": transient,
            "retailcrm_order_id": retailcrm_order_id,
            "komtet_order_id": komtet_order_id,
            "error": str(exc),
        }
    except Exception as exc:
        transient = _is_transient_error(exc)
        logger.exception("Unexpected error processing KOMTET callback")
        await _record_unexpected_failure(
            session,
            direction="komtet_to_crm",
            event_type="callback_error",
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            exc=exc,
            payload={**payload, "_transient_error": transient},
        )
        return {
            "status": "error",
            "transient_error": transient,
            "retailcrm_order_id": retailcrm_order_id,
            "komtet_order_id": komtet_order_id,
            "error": str(exc),
        }
    finally:
        if owns_crm and crm is not None:
            await crm.close()


async def _process_komtet_callback_locked(
    session: AsyncSession,
    *,
    cfg: RuntimeConfig,
    mappings: MappingLookup,
    crm: RetailCRMClient,
    payload: dict[str, Any],
    order_sync: Optional[OrderSync],
    retailcrm_order_id: int,
    komtet_order_id: Optional[int],
    raw_ext_id: Any,
    state: str,
    callback_items: list[dict[str, Any]],
    callback_payment_type: str,
    callback_amount: Any,
) -> dict[str, Any]:
    callback_hash = _compute_callback_hash(
        komtet_order_id,
        state,
        callback_payment_type,
        callback_amount,
        callback_items,
    )

    if (
        order_sync is not None
        and order_sync.last_komtet_callback_hash == callback_hash
        and order_sync.komtet_state == state
        and order_sync.sync_status in {"completed", "paid", "canceled"}
    ):
        await _add_sync_log(
            session,
            direction="komtet_to_crm",
            level="INFO",
            event_type="callback_duplicate_skipped",
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            message=f"Duplicate KOMTET callback (state='{state}') skipped idempotently",
        )
        await session.commit()
        return {
            "status": "skipped",
            "reason": "duplicate_callback",
            "retailcrm_order_id": retailcrm_order_id,
            "komtet_order_id": komtet_order_id,
        }

    now = _utc_now()
    if order_sync is None:
        order_sync = OrderSync(
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            komtet_external_id=str(raw_ext_id or retailcrm_order_id),
            sync_status="pending",
        )
        session.add(order_sync)

    if state != "done":
        # Non-final states do not modify RetailCRM, so they must NOT open the anti-loop
        # window (otherwise manager edits made right after a KOMTET status ping are lost).
        order_sync.komtet_order_id = komtet_order_id or order_sync.komtet_order_id
        order_sync.komtet_state = state or order_sync.komtet_state
        order_sync.last_komtet_callback_hash = callback_hash
        if state == "canceled":
            order_sync.sync_status = "canceled"
        await _add_sync_log(
            session,
            direction="komtet_to_crm",
            level="INFO",
            event_type="callback_state_update",
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            message=f"Received KOMTET callback with non-final state '{state}'",
            payload=payload,
        )
        await session.commit()
        return {
            "status": "ignored",
            "state": state,
            "retailcrm_order_id": retailcrm_order_id,
            "komtet_order_id": komtet_order_id,
        }

    # Fetch RetailCRM order for partial redemption & payment reconciliation
    lookup_site = order_sync.retailcrm_site or (cfg.retailcrm_default_site or None)
    crm_resp = await crm.get_order(retailcrm_order_id, by="id", site=lookup_site)
    crm_order = crm_resp.get("order") or {}
    if not crm_order:
        raise RetailCRMError(f"RetailCRM order {retailcrm_order_id} not found", status_code=404)

    # Compute the item patch before mutating anything (raises on unmatchable items).
    items_changed, edit_items_payload = _reconcile_partial_redemption(crm_order, callback_items)

    # Persist anti-loop timestamp before mutating RetailCRM so synchronous
    # RetailCRM triggers fired during edit_order/create_payment are ignored.
    order_sync.last_komtet_callback_at = now
    await session.commit()

    site = crm_order.get("site") or order_sync.retailcrm_site or (cfg.retailcrm_default_site or None)
    order_sync.retailcrm_order_number = (
        str(crm_order.get("number") or "") or order_sync.retailcrm_order_number
    )
    order_sync.retailcrm_site = site or order_sync.retailcrm_site

    # 1) Partial redemption: reconcile items if courier changed quantities or removed items
    if items_changed:
        await crm.edit_order(retailcrm_order_id, edit_items_payload, by="id", site=site)

    # 2) Compute final payable total and reconcile payment
    if callback_items:
        final_total = sum(
            (
                _cb_item_total(item)
                for item in callback_items
                if isinstance(item, dict) and _to_quantity(item.get("quantity", 0)) > _ZERO
            ),
            Decimal("0.00"),
        )
    elif callback_amount is not None:
        final_total = _to_decimal(callback_amount)
    else:
        final_total = _to_decimal(crm_order.get("totalSumm", 0))

    existing_payments = _extract_crm_payments(crm_order)
    paid_payments_sum = sum(
        (_to_decimal(p.get("amount", 0)) for p in existing_payments if p.get("status") == "paid"),
        Decimal("0.00"),
    )
    prepay_sum = _to_decimal(crm_order.get("prepaySum", 0))
    already_paid = max(paid_payments_sum, prepay_sum)
    remaining_to_pay = max(Decimal("0.00"), final_total - already_paid)
    overpaid = max(Decimal("0.00"), already_paid - final_total)

    target_crm_payment_type = _resolve_crm_payment_type(callback_payment_type, mappings)
    paid_at_str = datetime.now(get_app_timezone(cfg.app_timezone)).strftime("%Y-%m-%d %H:%M:%S")
    payment_action = "none"

    if remaining_to_pay > Decimal("0.00"):
        to_settle = _pick_payment_to_settle(existing_payments, target_crm_payment_type)
        if to_settle is not None:
            payment_patch: dict[str, Any] = {
                "id": to_settle["id"],
                "status": "paid",
                "amount": float(remaining_to_pay),
                "paidAt": paid_at_str,
            }
            if to_settle.get("type") != target_crm_payment_type:
                payment_patch["type"] = target_crm_payment_type
            await crm.update_payment(to_settle["id"], payment_patch, by="id", site=site)
            payment_action = "updated"
        else:
            payment_external_id = (
                f"komtet-{komtet_order_id}"
                if komtet_order_id is not None
                else f"komtet-order-{retailcrm_order_id}"
            )
            await crm.create_payment(
                {
                    "externalId": payment_external_id,
                    "order": {"id": retailcrm_order_id},
                    "type": target_crm_payment_type,
                    "status": "paid",
                    "amount": float(remaining_to_pay),
                    "paidAt": paid_at_str,
                    "comment": f"Оплата через КОМТЕТ Касса (заказ #{komtet_order_id})",
                },
                site=site,
            )
            payment_action = "created"

    # 3) Move RetailCRM order to final status (default: 'complete')
    if cfg.final_status and crm_order.get("status") != cfg.final_status:
        await crm.edit_order(
            retailcrm_order_id,
            {"status": cfg.final_status},
            by="id",
            site=site,
        )

    # 4) Update OrderSync (recording timestamp for anti-loop) & SyncLog
    order_sync.komtet_order_id = komtet_order_id or order_sync.komtet_order_id
    order_sync.komtet_state = "done"
    order_sync.sync_status = "completed"
    order_sync.last_komtet_callback_hash = callback_hash
    order_sync.last_komtet_callback_at = _utc_now()
    order_sync.last_synced_at = now
    order_sync.last_error = (
        f"Prepayment exceeds fiscalized total by {overpaid:.2f}; refund required"
        if overpaid > Decimal("0.00")
        else None
    )

    await _add_sync_log(
        session,
        direction="komtet_to_crm",
        level="WARNING" if overpaid > Decimal("0.00") else "INFO",
        event_type="callback_processed",
        retailcrm_order_id=retailcrm_order_id,
        komtet_order_id=komtet_order_id,
        message=(
            f"Processed KOMTET callback (state=done): payment={payment_action} "
            f"({float(remaining_to_pay):.2f} {target_crm_payment_type}), "
            f"items_changed={items_changed}, final_status={cfg.final_status}"
            + (f", OVERPAID by {overpaid:.2f} (refund required)" if overpaid > 0 else "")
        ),
        payload=payload,
    )
    await session.commit()
    return {
        "status": "processed",
        "retailcrm_order_id": retailcrm_order_id,
        "komtet_order_id": komtet_order_id,
        "items_changed": items_changed,
        "payment_action": payment_action,
        "payment_amount": float(remaining_to_pay),
        "payment_type": target_crm_payment_type,
        "final_status": cfg.final_status,
    }
