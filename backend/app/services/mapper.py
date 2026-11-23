"""Pure mapping and financial rounding helpers from RetailCRM orders to KOMTET Kassa."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Literal, Optional

from app.config import get_app_timezone
from app.services.settings_service import MappingLookup, RuntimeConfig

VALID_KOMTET_VATS = frozenset(
    {"no", "0", "5", "7", "10", "20", "22", "105", "107", "110", "120", "122"}
)
VALID_KOMTET_ORDER_PAYMENT_TYPES = frozenset({"card", "cash"})

_TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\D+(\d{1,2}:\d{2})")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_PENNY = Decimal("0.01")


def _to_decimal(value: Any, default: str = "0.00") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(_PENNY, rounding=ROUND_HALF_UP)


def normalize_phone(raw_phone: Optional[str]) -> str:
    """Normalize phone number into `+7XXXXXXXXXX` format when possible."""
    if not raw_phone:
        return ""

    # RetailCRM fields can contain multiple numbers separated by commas, semicolons,
    # slashes, pipes, newlines, or malformed `?` delimiters.
    fallback_short = ""
    for chunk in re.split(r"[,;/?|\n]+", str(raw_phone)):
        digits = re.sub(r"\D", "", chunk.strip())
        if len(digits) < 7 or len(digits) > 15:
            continue

        if len(digits) == 11 and digits.startswith("8"):
            return f"+7{digits[1:]}"
        if len(digits) == 11 and digits.startswith("7"):
            return f"+{digits}"
        if len(digits) == 10:
            return f"+7{digits}"
        if 10 <= len(digits) <= 15:
            return f"+{digits}"
        if not fallback_short:
            fallback_short = f"+{digits}"

    return fallback_short


def extract_client_phone(order: dict[str, Any]) -> str:
    """Extract and normalize buyer phone from RetailCRM order or customer."""
    candidates: list[Optional[str]] = [
        order.get("phone"),
        order.get("additionalPhone"),
    ]
    customer = order.get("customer")
    if isinstance(customer, dict):
        for phone_obj in customer.get("phones") or []:
            if isinstance(phone_obj, dict):
                candidates.append(phone_obj.get("number"))
            elif isinstance(phone_obj, str):
                candidates.append(phone_obj)

    for candidate in candidates:
        normalized = normalize_phone(candidate)
        if normalized:
            return normalized
    return ""


def extract_client_name(order: dict[str, Any]) -> str:
    """Assemble recipient full name from RetailCRM order or customer."""
    parts = [
        str(order.get(field) or "").strip()
        for field in ("lastName", "firstName", "patronymic")
    ]
    full_name = " ".join(p for p in parts if p)
    if full_name:
        return full_name

    customer = order.get("customer")
    if isinstance(customer, dict):
        cust_parts = [
            str(customer.get(field) or "").strip()
            for field in ("lastName", "firstName", "patronymic")
        ]
        return " ".join(p for p in cust_parts if p)
    return ""


def extract_client_email(order: dict[str, Any]) -> str:
    """Extract recipient email from RetailCRM order or customer."""
    email = str(order.get("email") or "").strip()
    if email:
        return email
    customer = order.get("customer")
    if isinstance(customer, dict):
        return str(customer.get("email") or "").strip()
    return ""


def build_delivery_address(order: dict[str, Any]) -> str:
    """Build delivery address string from `delivery.address.text` or structured fields."""
    delivery = order.get("delivery")
    if not isinstance(delivery, dict):
        return ""
    address = delivery.get("address")
    if not isinstance(address, dict):
        return ""

    text_addr = str(address.get("text") or "").strip()
    if text_addr:
        return text_addr

    parts: list[str] = []
    for key in ("index", "region", "city", "settlement"):
        val = str(address.get(key) or "").strip()
        if val and val not in parts:
            parts.append(val)

    street = str(address.get("street") or "").strip()
    street_type = str(address.get("streetType") or "").strip()
    if street:
        if street_type and not street.lower().startswith(street_type.lower()):
            parts.append(f"{street_type} {street}")
        else:
            parts.append(street)

    building = str(address.get("building") or address.get("house") or "").strip()
    if building:
        if building.lower().startswith(("д.", "д ", "дом ")):
            parts.append(building)
        else:
            parts.append(f"д. {building}")

    housing = str(address.get("housing") or "").strip()
    if housing:
        parts.append(f"корп. {housing}")

    block = str(address.get("block") or "").strip()
    if block:
        parts.append(f"стр. {block}")

    flat = str(address.get("flat") or "").strip()
    if flat:
        if flat.lower().startswith(("кв.", "кв ", "оф.")):
            parts.append(flat)
        else:
            parts.append(f"кв. {flat}")

    floor = str(address.get("floor") or "").strip()
    if floor:
        parts.append(f"эт. {floor}")

    porch = str(address.get("porch") or "").strip()
    if porch:
        parts.append(f"подъезд {porch}")

    return ", ".join(parts)


def _normalize_hhmm(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    match = _HHMM_RE.match(str(raw).strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _normalize_date(
    raw_date: Optional[str],
    reference_date: Optional[date],
    *,
    tz_name: str = "Europe/Moscow",
) -> str:
    if raw_date:
        text = str(raw_date).strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    if reference_date is not None:
        return reference_date.isoformat()
    return datetime.now(get_app_timezone(tz_name)).date().isoformat()


def build_delivery_window(
    order: dict[str, Any],
    *,
    default_from: str = "09:00",
    default_to: str = "21:00",
    reference_date: Optional[date] = None,
    tz_name: str = "Europe/Moscow",
) -> tuple[str, str]:
    """Return `(date_start, date_end)` formatted as `YYYY-MM-DD HH:MM`."""
    delivery = order.get("delivery") if isinstance(order.get("delivery"), dict) else {}
    date_str = _normalize_date(delivery.get("date"), reference_date, tz_name=tz_name)

    time_info = delivery.get("time") if isinstance(delivery.get("time"), dict) else {}
    start_hm = _normalize_hhmm(time_info.get("from"))
    end_hm = _normalize_hhmm(time_info.get("to"))

    if (not start_hm or not end_hm) and time_info.get("custom"):
        match = _TIME_RANGE_RE.search(str(time_info["custom"]))
        if match:
            start_hm = start_hm or _normalize_hhmm(match.group(1))
            end_hm = end_hm or _normalize_hhmm(match.group(2))

    fallback_from = _normalize_hhmm(default_from) or "09:00"
    fallback_to = _normalize_hhmm(default_to) or "21:00"

    start_hm = start_hm or fallback_from
    end_hm = end_hm or fallback_to
    if end_hm <= start_hm:
        end_hm = fallback_to if fallback_to > start_hm else "23:59"

    return f"{date_str} {start_hm}", f"{date_str} {end_hm}"


def map_vat_rate(
    raw_vat: Any,
    vat_mappings: Optional[dict[str, str]] = None,
    *,
    default_vat: str = "22",
) -> str:
    """Convert RetailCRM VAT representation into KOMTET VatRate string."""
    fallback = default_vat if default_vat in VALID_KOMTET_VATS else "22"
    if raw_vat is None or str(raw_vat).strip() == "":
        return fallback

    text = str(raw_vat).strip().lower()
    if vat_mappings:
        if text in vat_mappings and vat_mappings[text] in VALID_KOMTET_VATS:
            return vat_mappings[text]

    if text in {"none", "without", "no", "bez_nds", "without_vat", "без ндс", "не облагается"}:
        return vat_mappings.get("none", "no") if vat_mappings else "no"

    fraction_aliases = {
        "5/105": "105",
        "7/107": "107",
        "10/110": "110",
        "20/120": "120",
        "22/122": "122",
    }
    if text in fraction_aliases:
        return fraction_aliases[text]

    cleaned = text.replace("%", "").replace(",", ".").strip()
    try:
        numeric = Decimal(cleaned)
        if Decimal("0") < numeric < Decimal("1"):
            numeric = (numeric * Decimal("100")).quantize(Decimal("1"))
        else:
            numeric = numeric.quantize(Decimal("1"))
        candidate = str(int(numeric))
        if vat_mappings and candidate in vat_mappings and vat_mappings[candidate] in VALID_KOMTET_VATS:
            return vat_mappings[candidate]
        if candidate in VALID_KOMTET_VATS:
            return candidate
    except Exception:
        pass

    return fallback


def _extract_item_raw_total(item: dict[str, Any], quantity: Decimal) -> tuple[Decimal, bool]:
    """Compute item line total and whether order-level discounts are already baked in.

    Priority matches RetailCRM API v5 precision:
      1. `item["prices"]` exact per-tier breakdown (already includes all discounts)
      2. `item["summ"]` precomputed line total
      3. `(initialPrice - discountTotal) * quantity` (or manual item discounts if discountTotal == 0)
    """
    raw_prices = item.get("prices")
    if isinstance(raw_prices, list) and raw_prices:
        prices_total = Decimal("0.00")
        prices_qty = Decimal("0")
        for tier in raw_prices:
            if not isinstance(tier, dict):
                continue
            tier_qty = _to_decimal(tier.get("quantity", 0))
            if tier_qty <= Decimal("0"):
                continue
            tier_price = _to_decimal(tier.get("price", 0))
            prices_total += tier_price * tier_qty
            prices_qty += tier_qty
        if prices_qty > Decimal("0"):
            return max(Decimal("0.00"), _quantize_money(prices_total)), True

    if item.get("summ") is not None:
        return max(Decimal("0.00"), _quantize_money(_to_decimal(item["summ"]))), True

    initial_price = _to_decimal(item.get("initialPrice", item.get("price", 0)))
    discount_total = _to_decimal(item.get("discountTotal", 0))
    includes_discount_total = discount_total > Decimal("0.00")
    if not includes_discount_total:
        manual_amount = _to_decimal(item.get("discountManualAmount", 0))
        manual_percent = _to_decimal(item.get("discountManualPercent", 0))
        discount_total = manual_amount + (initial_price * manual_percent / Decimal("100"))

    effective_unit = max(Decimal("0.00"), initial_price - discount_total)
    return max(Decimal("0.00"), _quantize_money(effective_unit * quantity)), includes_discount_total


def _distribute_discount(item_totals: list[Decimal], discount: Decimal) -> list[Decimal]:
    """Proportionally distribute order-level discount across items to the exact kopeck."""
    total_sum = sum(item_totals, Decimal("0.00"))
    if discount <= Decimal("0.00") or total_sum <= Decimal("0.00"):
        return list(item_totals)

    capped_discount = min(discount, total_sum)
    return [
        line_total - share
        for line_total, share in zip(item_totals, _allocate_money(item_totals, capped_discount))
    ]


def _allocate_money(line_totals: list[Decimal], amount: Decimal) -> list[Decimal]:
    """Distribute whole kopecks by largest remainder, breaking ties by line order."""
    cents = int(_quantize_money(amount) / _PENNY)
    weights = [int(line_total / _PENNY) for line_total in line_totals]
    total_weight = sum(weights)
    if not total_weight:
        return [Decimal("0.00")] * len(weights)
    shares = [divmod(cents * weight, total_weight) for weight in weights]
    remaining = cents - sum(whole for whole, _ in shares)
    ranked = sorted(range(len(shares)), key=lambda index: -shares[index][1])
    bonus = set(ranked[:remaining])
    return [(whole + (index in bonus)) * _PENNY for index, (whole, _) in enumerate(shares)]


def _reconcile_totals_to_target(item_totals: list[Decimal], target_sum: Decimal) -> list[Decimal]:
    if not item_totals:
        return []
    current_sum = sum(item_totals)
    if current_sum >= target_sum:
        return _distribute_discount(item_totals, current_sum - target_sum)
    if not current_sum:
        return [*item_totals[:-1], target_sum]
    return [
        total + share
        for total, share in zip(item_totals, _allocate_money(item_totals, target_sum - current_sum))
    ]


def _split_item_for_exact_total(
    base_item: dict[str, Any],
    quantity: Decimal,
    target_total: Decimal,
    *,
    require_mark_code: bool = False,
    api_version: Literal["v1", "v2"],
) -> list[dict[str, Any]]:
    """Ensure `price * quantity == total` to the exact kopeck for integer quantities."""
    item_id = base_item["id"]
    name = base_item["name"]
    vat = base_item["vat"]
    item_type = base_item["type"]

    def build_entry(
        entry_id: Any,
        price: Decimal,
        qty: Decimal | int,
        total: Decimal,
    ) -> dict[str, Any]:
        qty_val: int | float = int(qty) if isinstance(qty, int) or qty == qty.to_integral_value() else float(qty)
        if api_version == "v1":
            entry: dict[str, Any] = {
                "order_item_id": entry_id,
                "name": name,
                "price": float(price),
                "quantity": qty_val,
                "total": float(total),
                "vat": vat,
                "type": item_type,
                "measure_name": "шт",
            }
            return entry

        # In KOMTET API v2 `id` is KOMTET's internal position ID; only pass RetailCRM's
        # item identifier in `external_id` (`"<id>"`, `"<id>_split"`, or `"delivery"`).
        return {
            "external_id": str(entry_id),
            "name": name,
            "price": float(price),
            "quantity": qty_val,
            "total": float(total),
            "vat": vat,
            "measure": 0,
            "type": item_type,
            "is_need_mark_code": item_type == "product" and require_mark_code,
        }

    if quantity == Decimal("1"):
        return [build_entry(item_id, target_total, 1, target_total)]

    is_integer_qty = quantity == quantity.to_integral_value()
    if is_integer_qty and quantity > Decimal("1"):
        int_qty = int(quantity)
        floor_unit = (target_total / quantity).quantize(_PENNY, rounding=ROUND_DOWN)
        remainder_cents = int(
            ((target_total - floor_unit * int_qty) * Decimal("100")).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        if remainder_cents == 0:
            return [build_entry(item_id, floor_unit, int_qty, target_total)]

        # Split into two lines differing by exactly 0.01 so price * quantity == total for both.
        qty_base = int_qty - remainder_cents
        entries: list[dict[str, Any]] = []
        if qty_base > 0:
            base_total = _quantize_money(floor_unit * qty_base)
            entries.append(build_entry(item_id, floor_unit, qty_base, base_total))
        rem_price = floor_unit + _PENNY
        rem_total = _quantize_money(rem_price * remainder_cents)
        rem_id = f"{item_id}_split" if qty_base > 0 else item_id
        entries.append(build_entry(rem_id, rem_price, remainder_cents, rem_total))
        return entries

    # Fractional weight/volume item
    unit_price = _quantize_money(target_total / quantity)
    return [build_entry(item_id, unit_price, quantity, target_total)]


def _extract_payments(order: dict[str, Any]) -> list[dict[str, Any]]:
    raw_payments = order.get("payments")
    if isinstance(raw_payments, dict):
        return [p for p in raw_payments.values() if isinstance(p, dict)]
    if isinstance(raw_payments, list):
        return [p for p in raw_payments if isinstance(p, dict)]
    return []


def _extract_courier_id(order: dict[str, Any]) -> Optional[str]:
    courier = order.get("courier")
    if isinstance(courier, dict) and courier.get("id") is not None:
        return str(courier["id"])

    delivery = order.get("delivery")
    if isinstance(delivery, dict):
        data = delivery.get("data")
        if isinstance(data, dict) and data.get("courierId") is not None:
            return str(data["courierId"])
    return None


def compute_payload_hash(payload: dict[str, Any]) -> str:
    """Return deterministic SHA-256 hex digest of a mapped KOMTET payload."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def map_retailcrm_order_to_komtet(
    order: dict[str, Any],
    config: RuntimeConfig,
    mappings: MappingLookup,
    *,
    reference_date: Optional[date] = None,
) -> tuple[dict[str, Any], str]:
    """Map a RetailCRM API v5 order into a KOMTET Kassa v1 or v2 delivery order payload."""
    api_version = config.komtet_api_version
    external_id = str(order.get("number") or order["id"]).strip()

    raw_items = order.get("items") or []
    prepared_items: list[dict[str, Any]] = []
    raw_totals: list[Decimal] = []
    any_item_has_discount_total = False

    for idx, item in enumerate(raw_items, start=1):
        qty = _to_decimal(item.get("quantity", 1))
        if qty <= Decimal("0"):
            continue

        offer = item.get("offer") if isinstance(item.get("offer"), dict) else {}
        item_name = str(
            offer.get("displayName")
            or offer.get("name")
            or item.get("productName")
            or f"Товар #{idx}"
        ).strip()
        raw_vat = item.get("vatRate")
        if raw_vat is None or str(raw_vat).strip() == "":
            raw_vat = offer.get("vatRate")

        item_id = item.get("id") or idx
        line_total, has_discount_total = _extract_item_raw_total(item, qty)
        if has_discount_total:
            any_item_has_discount_total = True
        raw_totals.append(line_total)
        prepared_items.append(
            {
                "id": item_id,
                "base_id": item_id,
                "name": item_name,
                "quantity": qty,
                "vat": map_vat_rate(
                    raw_vat,
                    mappings.vat_crm_to_komtet,
                    default_vat=config.default_vat,
                ),
                "type": "product",
            }
        )

    # Reconcile goods sum with order.summ (both discount and rounding-loss adjustments).
    # When order.summ is absent, do NOT re-subtract order-level discounts if item totals
    # already incorporate RetailCRM's distributed `discountTotal` / `prices` / `summ`.
    items_sum = sum(raw_totals, Decimal("0.00"))
    if order.get("summ") is not None and prepared_items:
        target_goods_sum = max(Decimal("0.00"), _quantize_money(_to_decimal(order["summ"])))
        adjusted_totals = _reconcile_totals_to_target(raw_totals, target_goods_sum)
    elif prepared_items and not any_item_has_discount_total:
        manual_amt = _to_decimal(order.get("discountManualAmount", 0))
        manual_pct = _to_decimal(order.get("discountManualPercent", 0))
        extra_discount = _quantize_money(
            manual_amt + (items_sum * manual_pct / Decimal("100"))
        )
        adjusted_totals = _distribute_discount(raw_totals, extra_discount)
    else:
        adjusted_totals = list(raw_totals)

    komtet_items: list[dict[str, Any]] = []
    for base_item, adj_total in zip(prepared_items, adjusted_totals):
        komtet_items.extend(
            _split_item_for_exact_total(
                base_item,
                base_item["quantity"],
                adj_total,
                require_mark_code=config.require_mark_code,
                api_version=api_version,
            )
        )

    # Delivery position if delivery cost > 0
    delivery = order.get("delivery") if isinstance(order.get("delivery"), dict) else {}
    delivery_cost = max(Decimal("0.00"), _quantize_money(_to_decimal(delivery.get("cost", 0))))
    if delivery_cost > Decimal("0.00"):
        delivery_vat = map_vat_rate(
            delivery.get("vatRate"),
            mappings.vat_crm_to_komtet,
            default_vat=config.default_vat,
        )
        komtet_items.extend(
            _split_item_for_exact_total(
                {
                    "id": "delivery",
                    "base_id": "delivery",
                    "name": "Доставка",
                    "vat": delivery_vat,
                    "type": "service",
                },
                Decimal("1"),
                delivery_cost,
                api_version=api_version,
            )
        )

    order_total = sum((_to_decimal(i["total"]) for i in komtet_items), Decimal("0.00"))

    # Prepayment & payment type resolution
    payments = _extract_payments(order)
    paid_payments_sum = sum(
        (_quantize_money(_to_decimal(p.get("amount", 0))) for p in payments if p.get("status") == "paid"),
        Decimal("0.00"),
    )
    prepay_sum = _quantize_money(_to_decimal(order.get("prepaySum", 0)))
    paid_sum = max(paid_payments_sum, prepay_sum)

    prepayment = min(paid_sum, order_total) if order_total > Decimal("0.00") else Decimal("0.00")
    is_fully_paid = order_total > Decimal("0.00") and prepayment >= order_total

    # KOMTET delivery order `payment_type` only accepts `"card"` or `"cash"`.
    # Prepayments (e.g. `oplachen` -> `"prepayment"`) are expressed via `prepayment` and
    # `is_pay_to_courier=False` / `is_paid=True`, never via `payment_type="prepayment"`.
    def _resolve_order_payment_type(crm_code: Optional[str]) -> Optional[str]:
        if not crm_code:
            return None
        mapped = mappings.payments_crm_to_komtet.get(crm_code)
        if mapped in VALID_KOMTET_ORDER_PAYMENT_TYPES:
            return mapped
        if crm_code in VALID_KOMTET_ORDER_PAYMENT_TYPES:
            return crm_code
        return None

    komtet_payment_type: Optional[str] = None
    unpaid_payments = [p for p in payments if p.get("status") != "paid"]
    for payment in unpaid_payments:
        komtet_payment_type = _resolve_order_payment_type(
            str(payment.get("type") or "").strip() or None
        )
        if komtet_payment_type:
            break
    if not komtet_payment_type:
        for payment in payments:
            komtet_payment_type = _resolve_order_payment_type(
                str(payment.get("type") or "").strip() or None
            )
            if komtet_payment_type:
                break
    if not komtet_payment_type and order.get("paymentType"):
        komtet_payment_type = _resolve_order_payment_type(str(order["paymentType"]).strip())
    if komtet_payment_type not in VALID_KOMTET_ORDER_PAYMENT_TYPES:
        komtet_payment_type = "card"

    # Delivery window, client, description, courier
    date_start, date_end = build_delivery_window(
        order,
        default_from=config.default_delivery_time_from,
        default_to=config.default_delivery_time_to,
        reference_date=reference_date,
        tz_name=config.app_timezone,
    )
    client_address = build_delivery_address(order) or "Адрес не указан"
    client_phone = extract_client_phone(order)
    client_name = extract_client_name(order)
    client_email = extract_client_email(order)

    comment_parts = [
        str(order.get("customerComment") or "").strip(),
        str(order.get("managerComment") or "").strip(),
    ]
    description = " | ".join(p for p in comment_parts if p)

    crm_courier_id = _extract_courier_id(order)
    komtet_courier_id: Optional[int] = None
    if crm_courier_id and crm_courier_id in mappings.couriers_crm_to_komtet:
        try:
            komtet_courier_id = int(mappings.couriers_crm_to_komtet[crm_courier_id])
        except ValueError:
            komtet_courier_id = None

    if api_version == "v1":
        payload: dict[str, Any] = {
            "order_id": external_id,
            "sno": int(config.tax_system),
            "state": "new",
            "is_paid": is_fully_paid,
            "prepayment": float(prepayment),
            "payment_type": komtet_payment_type,
            "description": description,
            "client_address": client_address,
            "client_phone": client_phone,
            "date_start": date_start,
            "date_end": date_end,
            "items": komtet_items,
        }
        if client_name:
            payload["client_name"] = client_name
        if client_email:
            payload["client_email"] = client_email
    else:
        date_start = datetime.strptime(date_start, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        date_end = datetime.strptime(date_end, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        client_obj: dict[str, Any] = {
            "address": client_address,
            "phone": client_phone,
        }
        if client_name:
            client_obj["name"] = client_name
        if client_email:
            client_obj["email"] = client_email

        company_obj: dict[str, Any] = {
            "payment_address": config.payment_address or config.retailcrm_url or "",
            "sno": int(config.tax_system),
        }
        if config.company_inn:
            company_obj["inn"] = config.company_inn

        payload = {
            "external_id": external_id,
            "state": "new",
            "is_pay_to_courier": not is_fully_paid,
            "prepayment": float(prepayment),
            "payment_type": komtet_payment_type,
            "description": description,
            "date_start": date_start,
            "date_end": date_end,
            "company": company_obj,
            "client": client_obj,
            "items": komtet_items,
        }

    if komtet_courier_id is not None:
        payload["courier_id"] = komtet_courier_id

    if config.komtet_callback_url:
        payload["callback_url"] = config.komtet_callback_url

    return payload, compute_payload_hash(payload)
