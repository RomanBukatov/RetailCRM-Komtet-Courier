"""Read-only discovery script for RetailCRM API v5 and KOMTET Kassa API.

Executes ONLY GET requests to verify credentials, inspect reference books,
check KOMTET queue status, fetch couriers/employees, and check whether the
`komtet_id` custom field already exists on orders in RetailCRM.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.clients.komtet import KomtetClient, KomtetError  # noqa: E402
from app.clients.retailcrm import RetailCRMClient, RetailCRMError  # noqa: E402
from app.config import get_settings  # noqa: E402


def _configure_utf8_console() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _dump(label: str, data: Any) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def discover_retailcrm(crm: RetailCRMClient) -> bool:
    """Inspect RetailCRM API v5 using strictly GET requests."""
    _print_header("RETAILCRM API v5 DISCOVERY (GET ONLY)")
    has_errors = False

    try:
        creds = await crm.check_credentials()
        _dump("Credentials (/api/credentials)", creds)
    except RetailCRMError as exc:
        has_errors = True
        print(f"[ERROR] Credentials check failed: {exc}")

    try:
        sites = await crm.get_sites()
        _dump("Sites (/api/v5/reference/sites)", sites)
    except RetailCRMError as exc:
        has_errors = True
        print(f"[ERROR] Failed to fetch sites: {exc}")

    try:
        statuses = await crm.get_statuses()
        _dump("Statuses (/api/v5/reference/statuses)", statuses)
    except RetailCRMError as exc:
        has_errors = True
        print(f"[ERROR] Failed to fetch statuses: {exc}")

    try:
        payment_types = await crm.get_payment_types()
        _dump("Payment Types (/api/v5/reference/payment-types)", payment_types)
    except RetailCRMError as exc:
        has_errors = True
        print(f"[ERROR] Failed to fetch payment types: {exc}")

    try:
        couriers = await crm.get_couriers()
        _dump("Couriers (/api/v5/reference/couriers)", couriers)
    except RetailCRMError as exc:
        has_errors = True
        print(f"[ERROR] Failed to fetch couriers: {exc}")

    try:
        komtet_field = await crm.get_custom_field("komtet_id", entity="order")
        exists = komtet_field is not None
        print(f"\n--- Custom Field 'komtet_id' on 'order' ---")
        print(f"Exists: {exists}")
        if komtet_field is not None:
            print(json.dumps(komtet_field, ensure_ascii=False, indent=2))
    except RetailCRMError as exc:
        has_errors = True
        print(f"[ERROR] Failed to inspect custom field 'komtet_id': {exc}")

    try:
        order_custom_fields = await crm.get_custom_fields(entity="order", limit=100, page=1)
        _dump("Order Custom Fields (/api/v5/custom-fields?filter[entity]=order)", order_custom_fields)
    except RetailCRMError as exc:
        has_errors = True
        print(f"[ERROR] Failed to list custom fields: {exc}")

    return not has_errors


async def discover_komtet(komtet: KomtetClient, queue_id: int | None) -> bool:
    """Inspect KOMTET Kassa API using strictly GET requests."""
    _print_header("KOMTET KASSA API DISCOVERY (GET ONLY)")
    has_errors = False

    if queue_id is None:
        print("[WARN] KOMTET_QUEUE_ID is not set in .env; skipping queue check.")
    else:
        for version in ("v1", "v2"):
            try:
                queue_info = await komtet.get_queue(queue_id, api_version=version)
                _dump(f"Queue {queue_id} (/api/shop/{version}/queues/{queue_id})", queue_info)
            except KomtetError as exc:
                if version == "v1":
                    has_errors = True
                print(f"[INFO/ERR] Queue {queue_id} on {version}: {exc}")

    # Official SDK endpoint: /api/shop/{v1|v2}/employees
    for version in ("v1", "v2"):
        try:
            employees = await komtet.get_employees(start=0, limit=100, api_version=version)
            _dump(f"All Employees (/api/shop/{version}/employees)", employees)
        except KomtetError as exc:
            if version == "v1":
                has_errors = True
            print(f"[INFO/ERR] Employees on {version}: {exc}")

        try:
            couriers = await komtet.get_couriers(start=0, limit=100, api_version=version)
            _dump(
                f"Courier Employees (/api/shop/{version}/employees?type=courier)",
                couriers,
            )
        except KomtetError as exc:
            print(f"[INFO/ERR] Couriers (type=courier) on {version}: {exc}")

    # Probe /api/shop/v1/couriers in read-only GET mode to compare with official SDK path
    try:
        legacy_couriers = await komtet.get_raw("/api/shop/v1/couriers")
        _dump("Probe (/api/shop/v1/couriers)", legacy_couriers)
    except KomtetError as exc:
        print(f"\n--- Probe (/api/shop/v1/couriers) ---\nNot available / error: {exc}")

    # Read-only check of delivery orders endpoint (v1 & v2)
    for version in ("v1", "v2"):
        try:
            orders = await komtet.get_orders(start=0, limit=5, api_version=version)
            _dump(f"Recent Orders (/api/shop/{version}/orders?start=0&limit=5)", orders)
        except KomtetError as exc:
            print(f"[INFO/ERR] Orders on {version}: {exc}")

    return not has_errors


async def main() -> int:
    _configure_utf8_console()
    settings = get_settings()

    if not settings.is_retailcrm_configured:
        print("[ERROR] RETAILCRM_URL or RETAILCRM_API_KEY is missing in .env")
        return 1
    if not settings.is_komtet_configured:
        print("[ERROR] KOMTET_SHOP_ID or KOMTET_SECRET_KEY is missing in .env")
        return 1

    print(f"RetailCRM URL: {settings.retailcrm_url}")
    print(f"KOMTET Base URL: {settings.komtet_base_url}")
    print(f"KOMTET Shop ID: {settings.komtet_shop_id}")
    print(f"KOMTET Queue ID: {settings.komtet_queue_id}")

    async with (
        RetailCRMClient(
            base_url=settings.retailcrm_url,
            api_key=settings.retailcrm_api_key.get_secret_value(),
            timeout=settings.http_timeout_seconds,
        ) as crm,
        KomtetClient(
            shop_id=settings.komtet_shop_id,
            secret_key=settings.komtet_secret_key.get_secret_value(),
            base_url=settings.komtet_base_url,
            api_version=settings.komtet_api_version,
            timeout=settings.http_timeout_seconds,
        ) as komtet,
    ):
        crm_ok = await discover_retailcrm(crm)
        komtet_ok = await discover_komtet(komtet, settings.komtet_queue_id)

    _print_header("DISCOVERY SUMMARY")
    print(f"RetailCRM read-only checks passed: {crm_ok}")
    print(f"KOMTET read-only checks passed:    {komtet_ok}")
    return 0 if (crm_ok and komtet_ok) else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
