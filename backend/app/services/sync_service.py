"""Bidirectional synchronization engine between RetailCRM and KOMTET Kassa."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.komtet import KomtetClient, KomtetError
from app.clients.retailcrm import RetailCRMClient, RetailCRMError
from app.config import Settings, get_app_timezone
from app.models import OrderSync, SyncLog
from app.services.mapper import (
    _allocate_money,
    _extract_item_raw_total,
    map_retailcrm_order_to_komtet,
)
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


def _compute_callback_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
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
    return re.sub(r"(?:_split|_m\d+)$", "", text)


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
         or `order_item_id`, with split suffixes stripped to re-aggregate rows).
         KOMTET's internal `cb_item["id"]` is NEVER matched against CRM IDs.
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
        if callback_items and not has_delivery_in_cb and not has_zero_goods_line:
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
    old_totals = [
        _extract_item_raw_total(item, _to_quantity(item.get("quantity", 0)))
        for item in crm_items
    ]
    # CRM line totals can already include the order-level discount. Add back
    # its allocated share before deriving a replacement manual order discount.
    net_totals = [total for total, _ in old_totals]
    net_sum = sum(net_totals, _ZERO)
    old_order_discount = crm_order_discount_amt
    if 0 < crm_order_discount_pct < 100:
        old_order_discount += net_sum * crm_order_discount_pct / (100 - crm_order_discount_pct)
    discount_shares = _allocate_money(net_totals, old_order_discount)

    for crm_item, (old_total, includes_discounts), order_share in zip(
        crm_items, old_totals, discount_shares
    ):
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
            price_type = crm_item.get("priceType")
            if isinstance(price_type, dict) and price_type.get("code"):
                item_patch["priceType"] = {"code": price_type["code"]}
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

            before_order_discount = old_total + (order_share if includes_discounts else _ZERO)
            item_unit_net = before_order_discount / old_qty if old_qty else _ZERO
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


def _receipt_items(payload: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    items = payload.get("items")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or "quantity" not in item:
            return None
        if not any(item.get(key) for key in ("external_id", "order_item_id", "name")):
            return None
        try:
            qty = _to_quantity(item["quantity"])
        except ValueError:
            return None
        value = item.get("total")
        if value is None:
            value = item.get("price")
        if qty > 0 and _receipt_amount(value) is None:
            return None
    return items


def _receipt_amount(value: Any) -> Optional[Decimal]:
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0 and amount == amount.quantize(_PENNY):
            return amount
    except InvalidOperation:
        pass
    return None


def _receipt_payments(payload: dict[str, Any]) -> Optional[dict[str, Decimal]]:
    receipt_info = payload.get("receipt_info")
    rows = receipt_info.get("payments") if isinstance(receipt_info, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("payments")
    if not isinstance(rows, list):
        return None
    payments: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in ("cash", "card", "prepayment"):
            return None
        amount = _receipt_amount(row.get("sum", row.get("amount")))
        if amount is None:
            return None
        payment_type = row["type"]
        payments[payment_type] = payments.get(payment_type, _ZERO) + amount

    if "prepayment" not in payments and payload.get("prepayment") is not None:
        prepayment = _receipt_amount(payload["prepayment"])
        if prepayment is None:
            return None
        payments["prepayment"] = prepayment
    return payments


def _receipt_total(items: list[dict[str, Any]]) -> Decimal:
    return sum(
        (_cb_item_total(item) for item in items if _to_quantity(item["quantity"]) > 0),
        Decimal("0.00"),
    )


def _payments_match_total(payments: dict[str, Decimal], total: Decimal) -> bool:
    credited = payments.get("prepayment", _ZERO)
    return sum(payments.values(), _ZERO) - max(_ZERO, credited - total) == total


def _item_quantities(items: list[dict[str, Any]]) -> dict[str, Decimal]:
    return {
        str(item["id"]): _to_quantity(item["quantity"])
        for item in items
        if _to_quantity(item["quantity"]) > 0
    }


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
    reconciliation_retry: bool = False,
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
            reconciliation_retry=reconciliation_retry,
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
    matches the visible order number or legacy internal ID).
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
    if owner in (None, "") or str(owner).strip() not in {str(order_id), order_sync.retailcrm_order_number}:
        return (
            None,
            (
                f"KOMTET order #{crm_komtet_id} has missing or mismatched external_id={owner!r}, "
                f"expected RetailCRM order #{order_id}; ignoring it"
            ),
            None,
        )

    order_sync.komtet_external_id = str(owner).strip()
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
        logger.warning(
            "[RCRM->KOMTET] order=%s komtet_id=%s writeback_failed: %s",
            order_id,
            komtet_id,
            exc,
        )
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
    reconciliation_retry: bool = False,
) -> dict[str, Any]:
    cfg = await load_runtime_config(session)
    mappings = await load_active_mappings(session)

    if not cfg.auto_sync_enabled and not force:
        logger.info(
            "[RCRM->KOMTET] order=%s site=%s status=- delivery=- -> action=skipped "
            "komtet_id=- reason=auto_sync_disabled",
            order_id,
            site or cfg.retailcrm_default_site or "-",
        )
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
    if reconciliation_retry and (
        order_sync is None
        or order_sync.sync_status != "error"
        or order_sync.komtet_state in {"done", "canceled"}
        or _ensure_utc(order_sync.updated_at) < now - timedelta(hours=48)
    ):
        return {"status": "skipped", "reason": "no_longer_retryable"}
    if not force and order_sync is not None:
        last_cb = _ensure_utc(order_sync.last_komtet_callback_at)
        if last_cb is not None:
            elapsed = (now - last_cb).total_seconds()
            if elapsed < cfg.anti_loop_seconds:
                logger.info(
                    "[RCRM->KOMTET] order=%s site=%s status=- delivery=- -> action=skipped "
                    "komtet_id=%s reason=anti_loop(%.1fs)",
                    order_id,
                    order_sync.retailcrm_site or site or "-",
                    order_sync.komtet_order_id or "-",
                    elapsed,
                )
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
        delivery_obj = order.get("delivery") if isinstance(order.get("delivery"), dict) else {}
        delivery_code = str(delivery_obj.get("code") or "").strip()
        resolved_site = (
            order.get("site")
            or (order_sync.retailcrm_site if order_sync is not None else None)
            or (site.strip() if site else None)
            or (cfg.retailcrm_default_site or None)
        )
        site_log = resolved_site or "-"
        status_log = status or "-"
        delivery_log = delivery_code or "-"
        order_number = str(order.get("number") or "").strip()
        crm_komtet_id = _extract_komtet_id_from_order(order, cfg.komtet_custom_field_code)

        if order_sync is None:
            order_sync = OrderSync(
                retailcrm_order_id=order_id,
                retailcrm_order_number=order_number or None,
                retailcrm_site=resolved_site,
                komtet_external_id=order_number or str(order_id),
                sync_status="pending",
            )
            session.add(order_sync)
        else:
            order_sync.retailcrm_order_number = order_number or order_sync.retailcrm_order_number
            order_sync.retailcrm_site = resolved_site or order_sync.retailcrm_site

        # Guard: an order already fiscalized (`komtet_state == "done"`) must NEVER be
        # deleted or overwritten in KOMTET, even if its CRM status is later moved to cancel.
        if not force and order_sync.komtet_state == "done":
            logger.info(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=already_done",
                order_id,
                site_log,
                status_log,
                delivery_log,
                order_sync.komtet_order_id or "-",
            )
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
                order_sync.sync_status = (
                    "pending_reconciliation" if reconciliation_retry else "completed"
                )
                order_sync.last_error = None
                logger.info(
                    "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                    "komtet_id=%s reason=already_done",
                    order_id,
                    site_log,
                    status_log,
                    delivery_log,
                    komtet_id,
                )
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
                logger.info(
                    "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=canceled "
                    "komtet_id=%s reason=cancel_status",
                    order_id,
                    site_log,
                    status_log,
                    delivery_log,
                    komtet_id,
                )
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

            logger.info(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=canceled_no_action",
                order_id,
                site_log,
                status_log,
                delivery_log,
                order_sync.komtet_order_id or "-",
            )
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
            or (order_sync.sync_status in {"completed", "refund_required"} and order_sync.komtet_state == "done")
        ):
            logger.info(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=final_status",
                order_id,
                site_log,
                status_log,
                delivery_log,
                komtet_id or "-",
            )
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
            logger.info(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=status_not_triggered",
                order_id,
                site_log,
                status_log,
                delivery_log,
                komtet_id or "-",
            )
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

        # Check allowed delivery types allowlist (empty list = allow all delivery types)
        if not ignore_status_filter and cfg.allowed_delivery_codes:
            if delivery_code not in cfg.allowed_delivery_codes:
                if order_sync in session.new:
                    session.expunge(order_sync)
                logger.info(
                    "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                    "komtet_id=%s reason=delivery_type_not_allowed",
                    order_id,
                    site_log,
                    status_log,
                    delivery_log,
                    komtet_id or "-",
                )
                await _add_sync_log(
                    session,
                    direction="crm_to_komtet",
                    level="INFO",
                    event_type="delivery_type_not_allowed",
                    retailcrm_order_id=order_id,
                    komtet_order_id=komtet_id,
                    message=(
                        f"CRM delivery type '{delivery_code or 'none'}' is not in "
                        f"allowed_delivery_codes ({', '.join(cfg.allowed_delivery_codes)})"
                    ),
                    payload={
                        "trigger_event": trigger_event,
                        "crm_status": status,
                        "delivery_code": delivery_code,
                        "allowed_delivery_codes": cfg.allowed_delivery_codes,
                    },
                )
                await session.commit()
                return {
                    "status": "skipped",
                    "reason": "delivery_type_not_allowed",
                    "retailcrm_order_id": order_id,
                    "delivery_code": delivery_code,
                }

        # Order canceled on the KOMTET side (e.g. by courier): never silently re-create it.
        if (
            not force
            and order_sync.komtet_order_id is not None
            and order_sync.sync_status == "canceled"
            and order_sync.komtet_state != _STATE_DELETED_BY_SYNC
        ):
            logger.warning(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=komtet_canceled",
                order_id,
                site_log,
                status_log,
                delivery_log,
                order_sync.komtet_order_id,
            )
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
            link_log_fn = logger.warning if komtet_id is None else logger.info
            link_log_fn(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=link_resolved "
                "komtet_id=%s reason=%s",
                order_id,
                site_log,
                status_log,
                delivery_log,
                komtet_id or crm_komtet_id or "-",
                link_note,
            )
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
        # Leave a recovered fiscalized order for reverse reconciliation during a retry.
        if komtet_id is not None and remote_state == "done":
            order_sync.komtet_order_id = komtet_id
            order_sync.komtet_state = "done"
            order_sync.sync_status = (
                "pending_reconciliation" if reconciliation_retry else "completed"
            )
            order_sync.last_error = None
            logger.info(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=already_done",
                order_id,
                site_log,
                status_log,
                delivery_log,
                komtet_id,
            )
            await _add_sync_log(
                session,
                direction="crm_to_komtet",
                level="INFO",
                event_type="anti_loop_skipped",
                retailcrm_order_id=order_id,
                komtet_order_id=komtet_id,
                message=(
                    f"KOMTET order #{komtet_id} is already 'done'; "
                    f"recorded sync status '{order_sync.sync_status}'"
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
            logger.warning(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=komtet_canceled",
                order_id,
                site_log,
                status_log,
                delivery_log,
                komtet_id,
            )
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
            order_sync.komtet_external_id = str(komtet_payload.get("external_id") or komtet_payload["order_id"])
            order_sync.komtet_state = str(created.get("state") or "new")
            order_sync.sync_status = "synced"
            order_sync.last_hash = payload_hash
            order_sync.last_synced_at = now
            order_sync.last_error = None
            logger.info(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=created "
                "komtet_id=%s reason=%s",
                order_id,
                site_log,
                status_log,
                delivery_log,
                new_komtet_id,
                reason,
            )
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
        if not force and not reconciliation_retry and order_sync.last_hash == payload_hash:
            logger.info(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=skipped "
                "komtet_id=%s reason=unchanged",
                order_id,
                site_log,
                status_log,
                delivery_log,
                komtet_id,
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
            return {
                "status": "skipped",
                "reason": "unchanged",
                "retailcrm_order_id": order_id,
                "komtet_order_id": komtet_id,
            }

        # Never send `state` on update: it would reset the courier workflow state in KOMTET.
        update_payload = {k: v for k, v in komtet_payload.items() if k != "state"}
        try:
            updated = await komtet.update_order(komtet_id, update_payload)
        except KomtetError as exc:
            if exc.status_code != 404:
                raise
            logger.warning(
                "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=recreate "
                "komtet_id=%s reason=komtet_order_missing",
                order_id,
                site_log,
                status_log,
                delivery_log,
                komtet_id,
            )
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
        order_sync.komtet_external_id = str(komtet_payload.get("external_id") or komtet_payload["order_id"])
        order_sync.komtet_state = str(updated.get("state") or order_sync.komtet_state or "new")
        order_sync.sync_status = (
            "pending_reconciliation" if reconciliation_retry and order_sync.komtet_state == "done" else "synced"
        )
        order_sync.last_hash = payload_hash
        order_sync.last_synced_at = now
        order_sync.last_error = None

        logger.info(
            "[RCRM->KOMTET] order=%s site=%s status=%s delivery=%s -> action=updated "
            "komtet_id=%s reason=%s",
            order_id,
            site_log,
            status_log,
            delivery_log,
            komtet_id,
            "forced" if force else "payload_changed",
        )
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

        return {
            "status": "updated",
            "retailcrm_order_id": order_id,
            "komtet_order_id": komtet_id,
        }

    except (KomtetError, RetailCRMError, ValueError) as exc:
        transient = _is_transient_error(exc)
        logger.error(
            "[SYNC] error direction=crm_to_komtet order=%s komtet_id=%s transient=%s: %s",
            order_id,
            komtet_id or "-",
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
        logger.error(
            "[SYNC] error unexpected direction=crm_to_komtet order=%s komtet_id=%s transient=%s: %s",
            order_id,
            komtet_id or "-",
            transient,
            exc,
            exc_info=True,
        )
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
    payload: Optional[dict[str, Any]] = None,
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

    body_text = raw_body.decode("utf-8", errors="replace") if raw_body else ""
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
        safe_payload = payload if isinstance(payload, dict) else {}
        logger.warning(
            "[KOMTET->RCRM] komtet_id=%s order=%s state=%s -> partial_edit=False "
            "payment=none final_status=- action=signature_invalid",
            safe_payload.get("id") or "-",
            safe_payload.get("external_id") or "-",
            safe_payload.get("state") or "-",
        )
        await _add_sync_log(
            session,
            direction="komtet_to_crm",
            level="ERROR",
            event_type="signature_invalid",
            message="Rejected KOMTET callback due to invalid X-HMAC-Signature",
            payload={
                "komtet_id": safe_payload.get("id"),
                "external_id": safe_payload.get("external_id"),
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
    komtet_client: Optional[KomtetClient] = None,
    komtet_factory: Callable[[RuntimeConfig], KomtetClient] = build_komtet_client,
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
            links = (await session.scalars(
                select(OrderSync).where(OrderSync.komtet_external_id == str(raw_ext_id)).limit(2)
            )).all()
            if len(links) > 1:
                raise ValueError(f"Ambiguous local KOMTET external_id {raw_ext_id!r}")
            order_sync = links[0] if links else None
            if order_sync is None:
                try:
                    ext_int = int(raw_ext_id)
                    order_sync = await session.scalar(
                        select(OrderSync).where(OrderSync.retailcrm_order_id == ext_int)
                    )
                except (TypeError, ValueError):
                    pass

        retailcrm_order_id = order_sync.retailcrm_order_id if order_sync is not None else None
        if crm is None:
            crm = build_retailcrm_client(cfg)

        if retailcrm_order_id is None and raw_ext_id is not None:
            filters = {"numbers": [str(raw_ext_id)]}
            if cfg.retailcrm_default_site:
                filters["sites"] = [cfg.retailcrm_default_site]
            matches = (await crm.get_orders(filters=filters, limit=2)).get("orders") or []
            if len(matches) > 1:
                raise ValueError(f"Ambiguous RetailCRM order number {raw_ext_id!r}")
            if matches:
                retailcrm_order_id = int(matches[0]["id"])
            elif str(raw_ext_id).isdecimal():
                try:
                    legacy_order = await crm.get_order(
                        int(raw_ext_id), by="id", site=cfg.retailcrm_default_site or None
                    )
                except RetailCRMError as exc:
                    if exc.status_code != 404:
                        raise
                else:
                    retailcrm_order_id = int(legacy_order["order"]["id"])

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
            logger.warning(
                "[KOMTET->RCRM] komtet_id=%s order=- state=%s -> partial_edit=False "
                "payment=none final_status=- action=order_not_found",
                komtet_order_id or "-",
                state or "-",
            )
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
                komtet=komtet_client,
                komtet_factory=komtet_factory,
            )

    except (RetailCRMError, KomtetError, ValueError) as exc:
        transient = _is_transient_error(exc)
        logger.error(
            "[SYNC] error direction=komtet_to_crm komtet_id=%s order=%s transient=%s: %s",
            komtet_order_id or "-",
            retailcrm_order_id or "-",
            transient,
            exc,
        )
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
        logger.error(
            "[SYNC] error unexpected direction=komtet_to_crm komtet_id=%s order=%s transient=%s: %s",
            komtet_order_id or "-",
            retailcrm_order_id or "-",
            transient,
            exc,
            exc_info=True,
        )
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
    komtet: Optional[KomtetClient],
    komtet_factory: Callable[[RuntimeConfig], KomtetClient],
    from_poll: bool = False,
) -> dict[str, Any]:
    if (
        order_sync is not None
        and order_sync.komtet_order_id is not None
        and komtet_order_id is not None
        and order_sync.komtet_order_id != komtet_order_id
    ):
        await _add_sync_log(
            session,
            direction="komtet_to_crm",
            level="WARNING",
            event_type="stale_callback_conflict",
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            message=f"Callback conflicts with active KOMTET order #{order_sync.komtet_order_id}",
        )
        await session.commit()
        return {"status": "skipped", "reason": "stale_callback_conflict"}

    callback_hash = _compute_callback_hash(payload)

    if (
        order_sync is not None
        and order_sync.last_komtet_callback_hash == callback_hash
        and order_sync.komtet_state == state
        and order_sync.sync_status in {"completed", "paid", "canceled", "refund_required"}
    ):
        logger.info(
            "[KOMTET->RCRM] komtet_id=%s order=%s state=%s -> partial_edit=False "
            "payment=none final_status=- action=skipped_duplicate",
            komtet_order_id or "-",
            retailcrm_order_id,
            state or "-",
        )
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
        logger.info(
            "[KOMTET->RCRM] komtet_id=%s order=%s state=%s -> partial_edit=False "
            "payment=none final_status=- action=ignored_non_final",
            komtet_order_id or "-",
            retailcrm_order_id,
            state or "-",
        )
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

    async def pending(reason: str) -> dict[str, Any]:
        order_sync.komtet_state = "done"
        order_sync.sync_status = "pending_reconciliation"
        order_sync.last_error = reason
        await _add_sync_log(
            session,
            direction="komtet_to_crm",
            level="WARNING",
            event_type="pending_reconciliation",
            retailcrm_order_id=retailcrm_order_id,
            komtet_order_id=komtet_order_id,
            message=reason,
            payload=payload,
        )
        await session.commit()
        return {"status": "pending_reconciliation", "reason": reason}

    callback_items = _receipt_items(payload)
    receipt_payments = _receipt_payments(payload)
    komtet_order_id = komtet_order_id or order_sync.komtet_order_id
    if not from_poll and (
        callback_items is None or receipt_payments is None
        or not _payments_match_total(receipt_payments, _receipt_total(callback_items))
    ):
        if komtet_order_id is None:
            return await pending("KOMTET order ID is missing")
        if komtet is None:
            async with komtet_factory(cfg) as client:
                detail = await client.get_order(komtet_order_id)
        else:
            detail = await komtet.get_order(komtet_order_id)
        if detail.get("state") != "done" or str(detail.get("id")) != str(komtet_order_id):
            return await pending("KOMTET order detail does not confirm the completed order")
        detailed_items = _receipt_items(detail)
        detailed_payments = _receipt_payments(detail)
        if detailed_items is not None:
            callback_items = detailed_items
        if detailed_payments is not None:
            receipt_payments = detailed_payments

    if callback_items is None or receipt_payments is None:
        return await pending("KOMTET did not provide complete items and payment breakdown")

    final_total = _receipt_total(callback_items)
    courier_payments = {
        kind: amount for kind, amount in receipt_payments.items()
        if kind != "prepayment" and amount > 0
    }
    courier_total = sum(courier_payments.values(), _ZERO)
    credited = receipt_payments.get("prepayment", _ZERO)
    if not _payments_match_total(receipt_payments, final_total):
        return await pending("KOMTET payment breakdown does not match the receipt total")

    lookup_site = order_sync.retailcrm_site or (cfg.retailcrm_default_site or None)
    crm_resp = await crm.get_order(retailcrm_order_id, by="id", site=lookup_site)
    crm_order = crm_resp.get("order") or {}
    if not crm_order:
        raise RetailCRMError(f"RetailCRM order {retailcrm_order_id} not found", status_code=404)

    items_changed, edit_items_payload = _reconcile_partial_redemption(crm_order, callback_items)
    expected_items = edit_items_payload.get("items", crm_order.get("items") or [])
    site = crm_order.get("site") or lookup_site
    order_sync.retailcrm_order_number = str(crm_order.get("number") or "") or order_sync.retailcrm_order_number
    order_sync.retailcrm_site = site or order_sync.retailcrm_site

    order_sync.last_komtet_callback_at = now
    await session.commit()
    if items_changed:
        await crm.edit_order(retailcrm_order_id, edit_items_payload, by="id", site=site)
        crm_order = (await crm.get_order(retailcrm_order_id, by="id", site=site)).get("order") or {}
    if (
        crm_order.get("totalSumm") is None
        or abs(_to_decimal(crm_order["totalSumm"]) - final_total) > _PENNY
        or _item_quantities(crm_order.get("items") or []) != _item_quantities(expected_items)
    ):
        return await pending("RetailCRM items or total do not match the fiscalized order")

    existing_payments = _extract_crm_payments(crm_order)
    payment_plan = {
        kind: (mappings.payments_komtet_to_crm.get(kind, default_type), amount)
        for kind, default_type in (("cash", "cash"), ("card", "bank-card"))
        if (amount := courier_payments.get(kind, _ZERO)) > 0
    }
    prefix = f"komtet-{komtet_order_id}" if komtet_order_id else f"komtet-order-{retailcrm_order_id}"
    paid_by_external_id = {
        p.get("externalId"): p for p in existing_payments if p.get("status") == "paid"
    }
    already_recorded = _ZERO
    for kind, (payment_type, amount) in payment_plan.items():
        existing = paid_by_external_id.get(f"{prefix}-{kind}") or paid_by_external_id.get(prefix)
        if existing:
            if existing.get("type") != payment_type or _to_decimal(existing.get("amount")) != amount:
                return await pending("Existing RetailCRM courier payment differs from the receipt")
            already_recorded += amount

    paid_sum = sum(
        (_to_decimal(p.get("amount")) for p in existing_payments if p.get("status") == "paid"),
        _ZERO,
    )
    already_paid = max(paid_sum, _to_decimal(crm_order.get("prepaySum")))
    paid_before_courier = already_paid - already_recorded
    overpaid = max(_ZERO, paid_before_courier + courier_total - final_total)
    if paid_before_courier + _PENNY < min(credited, final_total):
        return await pending("RetailCRM does not confirm the prepayment credited by KOMTET")

    actions: list[str] = []
    if not overpaid and courier_total > already_recorded:
        planned_types = {payment_type for payment_type, _ in payment_plan.values()}
        for payment in existing_payments:
            if (
                payment.get("id") is not None
                and payment.get("status") in ("", "not-paid", None)
                and payment.get("type") not in planned_types
            ):
                await crm.delete_payment(payment["id"])

        used_payment_ids = set()
        paid_at = datetime.now(get_app_timezone(cfg.app_timezone)).strftime("%Y-%m-%d %H:%M:%S")
        for kind, (payment_type, amount) in payment_plan.items():
            external_id = f"{prefix}-{kind}"
            if external_id in paid_by_external_id or prefix in paid_by_external_id:
                continue
            patch = {
                "externalId": external_id,
                "amount": float(amount),
                "status": "paid",
                "paidAt": paid_at,
            }
            candidate = next((
                p for p in existing_payments
                if p.get("id") is not None and p["id"] not in used_payment_ids
                and p.get("type") == payment_type
                and str(p.get("status") or "").strip() in _SETTLEABLE_PAYMENT_STATUSES
                and p.get("externalId") in (None, "", external_id)
            ), None)
            if candidate:
                await crm.update_payment(candidate["id"], patch, by="id", site=site)
                used_payment_ids.add(candidate["id"])
                actions.append("updated")
            else:
                await crm.create_payment(
                    {**patch, "order": {"id": retailcrm_order_id}, "type": payment_type},
                    site=site,
                )
                actions.append("created")

    payment_action = "+".join(actions) or "none"
    remaining_to_pay = courier_total - already_recorded
    target_crm_payment_type = "+".join(code for code, _ in payment_plan.values())
    final_status_applied = False
    if not overpaid and cfg.final_status and crm_order.get("status") != cfg.final_status:
        await crm.edit_order(retailcrm_order_id, {"status": cfg.final_status}, by="id", site=site)
        final_status_applied = True

    # 4) Update OrderSync (recording timestamp for anti-loop) & SyncLog
    order_sync.komtet_order_id = komtet_order_id or order_sync.komtet_order_id
    order_sync.komtet_state = "done"
    order_sync.sync_status = "refund_required" if overpaid else "completed"
    order_sync.last_komtet_callback_hash = callback_hash
    order_sync.last_komtet_callback_at = _utc_now()
    order_sync.last_synced_at = now
    order_sync.last_error = (
        f"Prepayment exceeds fiscalized total by {overpaid:.2f}; refund required"
        if overpaid > Decimal("0.00")
        else None
    )

    payment_summary = (
        f"{payment_action}({float(remaining_to_pay):.2f}/{target_crm_payment_type})"
        if payment_action != "none"
        else "none"
    )
    final_status_log = cfg.final_status if final_status_applied else f"{cfg.final_status or '-'}(unchanged)"
    action_log = f"processed_overpaid({overpaid:.2f})" if overpaid > Decimal("0.00") else "processed"
    done_log_fn = logger.warning if overpaid > Decimal("0.00") else logger.info
    done_log_fn(
        "[KOMTET->RCRM] komtet_id=%s order=%s state=%s -> partial_edit=%s "
        "payment=%s final_status=%s action=%s",
        komtet_order_id or "-",
        retailcrm_order_id,
        state,
        items_changed,
        payment_summary,
        final_status_log,
        action_log,
    )

    await _add_sync_log(
        session,
        direction="komtet_to_crm",
        level="WARNING" if overpaid > Decimal("0.00") else "INFO",
        event_type=("refund_required" if overpaid else
                    "order_status_polled" if from_poll else "callback_processed"),
        retailcrm_order_id=retailcrm_order_id,
        komtet_order_id=komtet_order_id,
        message=(
            f"Processed KOMTET {'order status from polling' if from_poll else 'callback'} "
            f"(state=done): payment={payment_action} "
            f"({float(remaining_to_pay):.2f} {target_crm_payment_type}), "
            f"items_changed={items_changed}, final_status={cfg.final_status}"
            + (f", OVERPAID by {overpaid:.2f} (refund required)" if overpaid > 0 else "")
        ),
        payload=payload,
    )
    await session.commit()
    return {
        "status": "refund_required" if overpaid else "processed",
        "retailcrm_order_id": retailcrm_order_id,
        "komtet_order_id": komtet_order_id,
        "items_changed": items_changed,
        "payment_action": payment_action,
        "payment_amount": float(remaining_to_pay),
        "payment_type": target_crm_payment_type,
        "final_status": cfg.final_status,
    }


# ---------------------------------------------------------------------------
# Background reconciliation
# ---------------------------------------------------------------------------


async def run_reconciliation_cycle(
    sessions: async_sessionmaker[AsyncSession],
    *,
    env_settings: Optional[Settings] = None,
    crm_client: Optional[RetailCRMClient] = None,
    komtet_client: Optional[KomtetClient] = None,
    after_ids: tuple[int, int] = (0, 0),
) -> tuple[int, int]:
    """Check a bounded batch in each direction and return the cursors for the next cycle."""
    retry_after_id, poll_after_id = after_ids
    now = _utc_now()
    poll_candidates = (
        OrderSync.komtet_order_id.is_not(None),
        OrderSync.updated_at >= now - timedelta(hours=72),
        OrderSync.sync_status.in_(("synced", "pending_reconciliation", "error")),
        # A receipt may already be finalized while its RetailCRM updates are still pending.
        or_(OrderSync.komtet_state.is_(None), OrderSync.komtet_state != "canceled"),
        or_(OrderSync.komtet_state.is_(None), OrderSync.komtet_state != "done",
            OrderSync.sync_status.in_(("pending_reconciliation", "error"))),
    )
    async with sessions() as session:
        cfg = await load_runtime_config(session, env_settings=env_settings)
        if not cfg.auto_sync_enabled:
            logger.info("Reconciliation cycle skipped: auto sync is disabled")
            return after_ids
        polls = (await session.execute(
            select(OrderSync.id, OrderSync.retailcrm_order_id)
            .where(*poll_candidates)
            .order_by(OrderSync.id <= poll_after_id, OrderSync.id)
            .limit(25)
        )).all()
        retries = (await session.execute(
            select(OrderSync.id, OrderSync.retailcrm_order_id)
            .where(
                OrderSync.sync_status == "error",
                OrderSync.updated_at >= now - timedelta(hours=48),
                or_(OrderSync.komtet_state.is_(None), OrderSync.komtet_state.not_in(("done", "canceled"))),
            )
            .order_by(OrderSync.id <= retry_after_id, OrderSync.id)
            .limit(20)
        )).all()

    # Check remote final states before replaying outbound changes for the same order.
    for row_id, order_id in polls:
        poll_after_id = row_id
        try:
            async with _order_lock(order_id), sessions() as session:
                row = await session.scalar(select(OrderSync).where(OrderSync.id == row_id, *poll_candidates))
                if row is None:
                    continue
                komtet_order_id = row.komtet_order_id
                if komtet_client is None:
                    async with build_komtet_client(cfg) as client:
                        detail = await client.get_order(komtet_order_id)
                else:
                    detail = await komtet_client.get_order(komtet_order_id)
                state = str(detail.get("state") or "").strip().lower()
                if state not in {"done", "canceled"}:
                    continue
                if str(detail.get("id")) != str(komtet_order_id):
                    raise ValueError(f"KOMTET returned a different order for #{komtet_order_id}")
                if state == "canceled":
                    row.komtet_state = "canceled"
                    row.sync_status = "canceled"
                    row.last_error = None
                    await _add_sync_log(
                        session, direction="komtet_to_crm", level="INFO", event_type="order_status_polled",
                        retailcrm_order_id=order_id, komtet_order_id=komtet_order_id,
                        message="KOMTET order state 'canceled' detected by polling",
                    )
                    await session.commit()
                    continue
                row.komtet_state = "done"
                row.sync_status = "pending_reconciliation"
                await session.commit()
                mappings = await load_active_mappings(session)
                crm = crm_client or build_retailcrm_client(cfg)
                try:
                    await _process_komtet_callback_locked(
                        session, cfg=cfg, mappings=mappings, crm=crm, payload=detail,
                        order_sync=row, retailcrm_order_id=order_id, komtet_order_id=komtet_order_id,
                        raw_ext_id=None, state=state, komtet=None, komtet_factory=build_komtet_client,
                        from_poll=True,
                    )
                finally:
                    if crm_client is None:
                        await crm.close()
        except Exception:
            logger.exception("Reconciliation could not check KOMTET status for RetailCRM order %s", order_id)

    for row_id, order_id in retries:
        retry_after_id = row_id
        try:
            async with sessions() as session:
                await sync_retailcrm_to_komtet(
                    session, order_id, trigger_event="auto_retry",
                    crm_client=crm_client, komtet_client=komtet_client,
                    reconciliation_retry=True,
                )
        except Exception:
            logger.exception("Reconciliation could not retry RetailCRM order %s", order_id)

    logger.info("Reconciliation cycle finished (status checks=%d, retry candidates=%d)", len(polls), len(retries))
    return retry_after_id, poll_after_id
