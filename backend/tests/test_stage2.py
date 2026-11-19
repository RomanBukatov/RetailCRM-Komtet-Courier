"""Comprehensive Stage 2 tests: mapper, bidirectional sync, anti-loop, webhooks, admin API."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.clients.komtet import KomtetClient, KomtetError  # noqa: E402
from app.clients.retailcrm import RetailCRMClient, RetailCRMError  # noqa: E402
from app.config import Settings, get_app_timezone  # noqa: E402
from app.database import create_engine, get_db, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Mapping, OrderSync, Setting, SyncLog  # noqa: E402
from app.routers import admin_api, webhooks  # noqa: E402
from app.security import validate_outbound_url  # noqa: E402
from app.services.mapper import (  # noqa: E402
    build_delivery_address,
    build_delivery_window,
    map_retailcrm_order_to_komtet,
    map_vat_rate,
    normalize_phone,
)
from app.services.settings_service import (  # noqa: E402
    MappingLookup,
    RuntimeConfig,
    load_active_mappings,
    load_runtime_config,
)
from app.services.sync_service import (  # noqa: E402
    _reconcile_partial_redemption,
    sync_komtet_to_retailcrm,
    sync_retailcrm_to_komtet,
)


def _test_env_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "app_env": "test",
        "app_timezone": "Europe/Moscow",
        "retailcrm_url": "https://example.retailcrm.ru",
        "retailcrm_api_key": "test-crm-api-key-9999",
        "retailcrm_default_site": "main-site",
        "komtet_shop_id": "test-shop-id",
        "komtet_secret_key": "test-komtet-secret-8888",
        "komtet_queue_id": 12345,
        "komtet_api_version": "v2",
        "komtet_callback_url": "https://sync.example.com/webhooks/komtet",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _sample_config(**overrides: Any) -> RuntimeConfig:
    defaults: dict[str, Any] = {
        "retailcrm_url": "https://example.retailcrm.ru",
        "retailcrm_api_key": "test-crm-api-key-9999",
        "retailcrm_default_site": "main-site",
        "komtet_base_url": "https://kassa.komtet.ru",
        "komtet_shop_id": "test-shop-id",
        "komtet_secret_key": "test-komtet-secret-8888",
        "komtet_queue_id": 12345,
        "komtet_api_version": "v2",
        "tax_system": 0,
        "default_vat": "22",
        "payment_address": "https://shop.example.com",
        "company_inn": "123456789012",
        "trigger_statuses": ["new", "paid", "send-to-delivery", "delivering"],
        "cancel_statuses": ["cancel-other", "otklonen", "no-product"],
        "final_status": "complete",
        "default_delivery_time_from": "09:00",
        "default_delivery_time_to": "21:00",
        "komtet_callback_url": "https://sync.example.com/webhooks/komtet",
        "verify_komtet_signature": True,
        "anti_loop_seconds": 15,
        "auto_sync_enabled": True,
        "komtet_custom_field_code": "komtet_id",
        "app_timezone": "Europe/Moscow",
    }
    defaults.update(overrides)
    return RuntimeConfig(**defaults)


def _sample_mappings() -> MappingLookup:
    return MappingLookup(
        couriers_crm_to_komtet={"4": "701"},
        payments_crm_to_komtet={
            "cash": "cash",
            "bank-card": "card",
            "oplachen": "prepayment",
        },
        payments_komtet_to_crm={
            "cash": "cash",
            "card": "bank-card",
            "prepayment": "oplachen",
        },
        vat_crm_to_komtet={
            "10": "10",
            "22": "22",
            "20": "20",
            "0": "0",
            "none": "no",
        },
    )


# ---------------------------------------------------------------------------
# 1. Mapper unit tests
# ---------------------------------------------------------------------------


def test_mapper_phone_address_delivery_window_and_vat() -> None:
    assert normalize_phone("8 (999) 123-45-67") == "+79991234567"
    assert normalize_phone("9991234567, 89001112233") == "+79991234567"
    assert normalize_phone("+7 916 555-00-11") == "+79165550011"

    order_structured = {
        "delivery": {
            "date": "2026-11-20",
            "address": {
                "city": "Москва",
                "street": "Ленина",
                "building": "10",
                "housing": "2",
                "flat": "45",
            },
        }
    }
    assert build_delivery_address(order_structured) == "Москва, Ленина, д. 10, корп. 2, кв. 45"
    assert build_delivery_address({"delivery": {"address": {"text": "г. Москва, ул. Мира 1"}}}) == (
        "г. Москва, ул. Мира 1"
    )

    # Default time window when time is omitted
    assert build_delivery_window(
        {"delivery": {"date": "2026-11-20"}},
        default_from="09:00",
        default_to="21:00",
    ) == ("2026-11-20 09:00", "2026-11-20 21:00")

    # Custom time string fallback
    assert build_delivery_window(
        {"delivery": {"date": "2026-11-20", "time": {"custom": "с 14:00 до 18:30"}}},
    ) == ("2026-11-20 14:00", "2026-11-20 18:30")

    # Missing date falls back to reference_date
    assert build_delivery_window({}, reference_date=date(2026, 11, 19)) == (
        "2026-11-19 09:00",
        "2026-11-19 21:00",
    )

    # Flexible VAT mapping
    vat_map = {"10": "10", "22": "22", "none": "no"}
    assert map_vat_rate("10.00", vat_map, default_vat="22") == "10"
    assert map_vat_rate("22%", vat_map, default_vat="10") == "22"
    assert map_vat_rate(0.20, vat_map, default_vat="22") == "20"
    assert map_vat_rate("without", vat_map, default_vat="22") == "no"
    assert map_vat_rate("22/122", vat_map, default_vat="22") == "122"
    assert map_vat_rate(None, vat_map, default_vat="22") == "22"


def test_mapper_discounts_penny_exact_split_delivery_and_prepayment() -> None:
    """Order discount creating odd cents per unit must split into exact price*quantity==total lines."""
    crm_order = {
        "id": 5001,
        "number": "5001A",
        "site": "main-site",
        "status": "send-to-delivery",
        "firstName": "Иван",
        "lastName": "Петров",
        "phone": "8 (916) 111-22-33",
        "email": "ivan@example.com",
        "customerComment": "Домофон 12",
        "summ": 290.00,  # 3 * 100.00 = 300.00 -> extra 10.00 order discount -> 290.00
        "courier": {"id": 4},
        "delivery": {
            "cost": 350.00,
            "date": "2026-11-21",
            "time": {"from": "10:00", "to": "14:00"},
            "address": {"text": "г. Москва, ул. Тверская, д. 5"},
        },
        "items": [
            {
                "id": 91,
                "initialPrice": 100.00,
                "discountTotal": 0,
                "quantity": 3,
                "offer": {"id": 555, "name": "Корм сухой", "vatRate": "10.00"},
            }
        ],
        "payments": {
            "1": {"id": 1, "type": "oplachen", "status": "paid", "amount": 200.00},
            "2": {"id": 2, "type": "bank-card", "status": "not-paid", "amount": 440.00},
        },
    }

    payload, payload_hash = map_retailcrm_order_to_komtet(
        crm_order,
        _sample_config(komtet_api_version="v2"),
        _sample_mappings(),
    )

    assert payload["external_id"] == "5001"
    assert payload["company"]["sno"] == 0
    assert payload["courier_id"] == 701
    assert payload["client"]["phone"] == "+79161112233"
    assert payload["date_start"] == "2026-11-21 10:00"
    assert payload["date_end"] == "2026-11-21 14:00"
    assert payload["prepayment"] == 200.0
    assert payload["is_pay_to_courier"] is True
    assert payload["payment_type"] == "card"
    assert len(payload_hash) == 64

    items = payload["items"]
    # Product split into 2 lines (1 * 96.66 + 2 * 96.67 = 290.00) + 1 delivery service line (350.00)
    assert len(items) == 3
    for item in items:
        assert Decimal(str(item["price"])) * Decimal(str(item["quantity"])) == Decimal(
            str(item["total"])
        )

    product_lines = [i for i in items if i["type"] == "product"]
    assert sum(Decimal(str(i["total"])) for i in product_lines) == Decimal("290.00")
    assert all(i["vat"] == "10" for i in product_lines)
    assert [i["external_id"] for i in product_lines] == ["91", "91_split"]
    assert all("id" not in i for i in items)

    delivery_lines = [i for i in items if i["type"] == "service"]
    assert len(delivery_lines) == 1
    assert delivery_lines[0]["name"] == "Доставка"
    assert delivery_lines[0]["total"] == 350.0
    assert delivery_lines[0]["vat"] == "22"

    # Also verify KOMTET v1 payload format
    payload_v1, _ = map_retailcrm_order_to_komtet(
        crm_order,
        _sample_config(komtet_api_version="v1"),
        _sample_mappings(),
    )
    assert payload_v1["order_id"] == 5001
    assert payload_v1["sno"] == 0
    assert payload_v1["is_paid"] is False
    assert payload_v1["client_phone"] == "+79161112233"
    assert payload_v1["items"][0]["order_item_id"] == 91


# ---------------------------------------------------------------------------
# 2. Bidirectional Sync, Anti-loop, Idempotency & Error Logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_sync_create_update_cancel_and_srv00_error(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'sync.db').as_posix()}"
    engine = create_engine(db_url)
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    crm_state: dict[str, Any] = {
        "order": {
            "id": 777,
            "number": "777A",
            "site": "main-site",
            "status": "send-to-delivery",
            "firstName": "Анна",
            "phone": "89990001122",
            "summ": 1000.0,
            "delivery": {
                "cost": 200.0,
                "date": "2026-11-22",
                "address": {"text": "Москва, Арбат 1"},
            },
            "items": [
                {
                    "id": 11,
                    "initialPrice": 500.0,
                    "discountTotal": 0,
                    "quantity": 2,
                    "offer": {"id": 101, "name": "Игрушка", "vatRate": "22.00"},
                }
            ],
            "payments": {"10": {"id": 10, "type": "bank-card", "status": "not-paid", "amount": 1200.0}},
            "customFields": {},
        },
        "edits": [],
    }
    komtet_calls: list[tuple[str, str, Any]] = []
    komtet_mode = {"fail_srv00": False}

    def crm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/orders/777" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "order": crm_state["order"]})
        if request.url.path == "/api/v5/orders/777/edit" and request.method == "POST":
            form = parse_qs(request.content.decode("utf-8"))
            order_patch = json.loads(form["order"][0])
            crm_state["edits"].append(order_patch)
            if "customFields" in order_patch:
                crm_state["order"]["customFields"].update(order_patch["customFields"])
            return httpx.Response(200, json={"success": True, "id": 777})
        return httpx.Response(404, json={"success": False})

    def komtet_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        komtet_calls.append((request.method, request.url.path, body))
        if komtet_mode["fail_srv00"]:
            return httpx.Response(
                403,
                json={
                    "title": "Нет активной услуги",
                    "code": "SRV00",
                    "description": "Необходимо активировать услугу",
                },
            )
        if request.method == "POST" and request.url.path == "/api/shop/v2/orders":
            return httpx.Response(200, json={"id": 8881, "external_id": "777", "state": "new"})
        if request.method == "PUT" and request.url.path == "/api/shop/v2/orders/8881":
            return httpx.Response(200, json={"id": 8881, "external_id": "777", "state": "new"})
        if request.method == "DELETE" and request.url.path == "/api/shop/v2/orders/8881":
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async with (
        session_maker() as db,
        httpx.AsyncClient(transport=httpx.MockTransport(crm_handler)) as crm_http,
        httpx.AsyncClient(transport=httpx.MockTransport(komtet_handler)) as komtet_http,
    ):
        crm_cli = RetailCRMClient("https://example.retailcrm.ru", "test-key", client=crm_http)
        komtet_cli = KomtetClient(
            "test-shop-id", "test-komtet-secret-8888", api_version="v2", client=komtet_http
        )

        # 1) Create in KOMTET & save integer customFields.komtet_id in RetailCRM
        res1 = await sync_retailcrm_to_komtet(
            db, 777, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res1["status"] == "created"
        assert res1["komtet_order_id"] == 8881
        assert crm_state["edits"][0] == {"customFields": {"komtet_id": 8881}}
        assert isinstance(crm_state["edits"][0]["customFields"]["komtet_id"], int)

        # 2) Same payload again -> skipped due to identical SHA-256 hash
        res2 = await sync_retailcrm_to_komtet(
            db, 777, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res2["status"] == "skipped"
        assert res2["reason"] == "unchanged"

        # 3) Order changed (e.g., comment updated) -> update_order called
        crm_state["order"]["customerComment"] = "Позвонить за час"
        res3 = await sync_retailcrm_to_komtet(
            db, 777, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res3["status"] == "updated"
        assert komtet_calls[-1][0] == "PUT"

        # 4) KOMTET 403 SRV00 error is logged in sync_logs without crashing
        crm_state["order"]["customerComment"] = "Другой комментарий"
        komtet_mode["fail_srv00"] = True
        res4 = await sync_retailcrm_to_komtet(
            db, 777, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res4["status"] == "error"
        assert "SRV00" in res4["error"]

        err_log = await db.scalar(
            select(SyncLog)
            .where(SyncLog.retailcrm_order_id == 777, SyncLog.level == "ERROR")
            .order_by(SyncLog.id.desc())
        )
        assert err_log is not None
        assert "SRV00" in err_log.message

        # 5) Cancel status deletes order in KOMTET
        komtet_mode["fail_srv00"] = False
        crm_state["order"]["status"] = "cancel-other"
        res5 = await sync_retailcrm_to_komtet(
            db, 777, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res5["status"] == "canceled"
        assert komtet_calls[-1][0] == "DELETE"

    await engine.dispose()


@pytest.mark.asyncio
async def test_reverse_sync_partial_redemption_payment_idempotency_and_antiloop(
    tmp_path: Path,
) -> None:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'reverse.db').as_posix()}"
    engine = create_engine(db_url)
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    crm_order = {
        "id": 900,
        "number": "900",
        "site": "main-site",
        "status": "delivering",
        "summ": 1500.0,
        "totalSumm": 1500.0,
        "delivery": {"cost": 0},
        "items": [
            {
                "id": 101,
                "initialPrice": 500.0,
                "discountManualAmount": 0,
                "discountManualPercent": 0,
                "quantity": 2,
                "offer": {"id": 1, "name": "Поводок"},
            },
            {
                "id": 102,
                "initialPrice": 500.0,
                "discountManualAmount": 0,
                "discountManualPercent": 0,
                "quantity": 1,
                "offer": {"id": 2, "name": "Миска"},
            },
        ],
        "payments": {},
        "customFields": {"komtet_id": 4455},
    }
    crm_calls: list[tuple[str, str, dict[str, Any]]] = []

    def crm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/orders/900" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "order": crm_order})
        if request.url.path == "/api/v5/orders/900/edit" and request.method == "POST":
            form = parse_qs(request.content.decode("utf-8"))
            patch = json.loads(form["order"][0])
            crm_calls.append(("edit_order", request.url.path, patch))
            if "status" in patch:
                crm_order["status"] = patch["status"]
            return httpx.Response(200, json={"success": True, "id": 900})
        if request.url.path == "/api/v5/orders/payments/create" and request.method == "POST":
            form = parse_qs(request.content.decode("utf-8"))
            pay = json.loads(form["payment"][0])
            crm_calls.append(("create_payment", request.url.path, pay))
            return httpx.Response(201, json={"success": True, "id": 321})
        if request.url.path == "/api/v5/orders/payments/55/edit" and request.method == "POST":
            form = parse_qs(request.content.decode("utf-8"))
            pay = json.loads(form["payment"][0])
            crm_calls.append(("update_payment", request.url.path, pay))
            return httpx.Response(200, json={"success": True, "id": 55})
        return httpx.Response(404)

    # Courier delivered only 1 unit of item 101 (instead of 2) and removed item 102; paid by card.
    callback_payload = {
        "id": 4455,
        "external_id": "900",
        "state": "done",
        "payment_type": "card",
        "amount": 500.0,
        "items": [
            {
                "id": 101,
                "external_id": "101",
                "name": "Поводок",
                "price": 500.0,
                "quantity": 1,
                "total": 500.0,
                "type": "product",
            }
        ],
    }
    raw_body = json.dumps(callback_payload).encode("utf-8")
    callback_url = "https://sync.example.com/webhooks/komtet"
    valid_sig = KomtetClient.compute_signature(
        "test-komtet-secret-8888", "POST", callback_url, raw_body.decode("utf-8")
    )

    async with (
        session_maker() as db,
        httpx.AsyncClient(transport=httpx.MockTransport(crm_handler)) as crm_http,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200))) as k_http,
    ):
        db.add(
            OrderSync(
                retailcrm_order_id=900,
                komtet_order_id=4455,
                komtet_external_id="900",
                sync_status="synced",
            )
        )
        await db.commit()

        crm_cli = RetailCRMClient("https://example.retailcrm.ru", "test-key", client=crm_http)
        komtet_cli = KomtetClient("test-shop-id", "test-komtet-secret-8888", client=k_http)

        # Bad signature must fail
        with pytest.raises(PermissionError):
            await sync_komtet_to_retailcrm(
                db,
                callback_payload,
                signature="invalid",
                raw_url=callback_url,
                raw_body=raw_body,
                crm_client=crm_cli,
            )

        # Valid signature: partial redemption + create paid bank-card payment + status=complete
        res = await sync_komtet_to_retailcrm(
            db,
            callback_payload,
            signature=valid_sig,
            raw_url=callback_url,
            raw_body=raw_body,
            crm_client=crm_cli,
        )
        assert res["status"] == "processed"
        assert res["items_changed"] is True
        assert res["payment_action"] == "created"
        assert res["payment_amount"] == 500.0
        assert res["payment_type"] == "bank-card"
        assert res["final_status"] == "complete"

        assert crm_calls[0][0] == "edit_order"
        assert crm_calls[0][2]["items"] == [
            {
                "id": 101,
                "quantity": 1,
                "offer": {"id": 1},
                "initialPrice": 500.0,
                "discountManualAmount": 0,
                "discountManualPercent": 0,
            }
        ]
        assert crm_calls[1][0] == "create_payment"
        assert crm_calls[1][2]["type"] == "bank-card"
        assert crm_calls[1][2]["status"] == "paid"
        assert crm_calls[1][2]["amount"] == 500.0
        assert crm_calls[2] == ("edit_order", "/api/v5/orders/900/edit", {"status": "complete"})

        # Idempotency: exact same callback again -> skipped without duplicating payment
        calls_before = len(crm_calls)
        dup_res = await sync_komtet_to_retailcrm(
            db,
            callback_payload,
            signature=valid_sig,
            raw_url=callback_url,
            raw_body=raw_body,
            crm_client=crm_cli,
        )
        assert dup_res["status"] == "skipped"
        assert dup_res["reason"] == "duplicate_callback"
        assert len(crm_calls) == calls_before

        # Anti-loop: RetailCRM webhook fired right after callback is skipped
        loop_res = await sync_retailcrm_to_komtet(
            db,
            900,
            trigger_event="order_edit",
            crm_client=crm_cli,
            komtet_client=komtet_cli,
        )
        assert loop_res["status"] == "skipped"
        assert loop_res["reason"] == "anti_loop"

        # Existing unpaid cash payment -> updated to paid (not duplicated)
        crm_order["status"] = "delivering"
        crm_order["items"] = [crm_order["items"][0]]
        crm_order["items"][0]["quantity"] = 1
        crm_order["payments"] = {"55": {"id": 55, "type": "cash", "status": "not-paid", "amount": 500.0}}
        cash_cb = {
            "id": 4456,
            "external_id": "901",
            "state": "done",
            "payment_type": "cash",
            "amount": 500.0,
            "items": [
                {
                    "id": 101,
                    "external_id": "101",
                    "name": "Поводок",
                    "price": 500.0,
                    "quantity": 1,
                    "total": 500.0,
                    "type": "product",
                }
            ],
        }
        # Point order 901 to same mock handler via 900 path? Use 900 with reset OrderSync for clean test:
        osync = await db.scalar(select(OrderSync).where(OrderSync.retailcrm_order_id == 900))
        assert osync is not None
        osync.last_komtet_callback_hash = None
        await db.commit()
        cash_cb["id"] = 4455
        cash_cb["external_id"] = "900"
        cash_body = json.dumps(cash_cb).encode("utf-8")
        cash_sig = KomtetClient.compute_signature(
            "test-komtet-secret-8888", "POST", callback_url, cash_body.decode("utf-8")
        )
        cash_res = await sync_komtet_to_retailcrm(
            db,
            cash_cb,
            signature=cash_sig,
            raw_url=callback_url,
            raw_body=cash_body,
            crm_client=crm_cli,
        )
        assert cash_res["payment_action"] == "updated"
        assert crm_calls[-2][0] == "update_payment"
        assert crm_calls[-2][2]["status"] == "paid"

    await engine.dispose()


# ---------------------------------------------------------------------------
# 3. FastAPI Webhooks & Admin API endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhooks_and_admin_api_endpoints(tmp_path: Path) -> None:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}"
    engine = create_engine(db_url)
    test_settings = _test_env_settings()
    await init_db(engine, seed=True, env_settings=test_settings)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    app = create_app(test_settings)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    def crm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/credentials":
            return httpx.Response(
                200, json={"success": True, "sitesAvailable": ["main-site"]}
            )
        if request.url.path == "/api/v5/custom-fields/order/komtet_id":
            return httpx.Response(
                200,
                json={"success": True, "customField": {"code": "komtet_id", "type": "integer"}},
            )
        if request.url.path == "/api/v5/reference/statuses":
            return httpx.Response(
                200,
                json={"success": True, "statuses": {"new": {"code": "new", "name": "Открыт"}}},
            )
        if request.url.path == "/api/v5/reference/payment-types":
            return httpx.Response(
                200,
                json={"success": True, "paymentTypes": {"cash": {"code": "cash", "name": "Наличные"}}},
            )
        if request.url.path == "/api/v5/reference/couriers":
            return httpx.Response(
                200,
                json={"success": True, "couriers": [{"id": 1, "firstName": "Александр"}]},
            )
        if request.url.path == "/api/v5/reference/sites":
            return httpx.Response(
                200,
                json={"success": True, "sites": {"main-site": {"code": "main-site", "name": "Main Store"}}},
            )
        if request.url.path == "/api/v5/orders/1200" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "order": {
                        "id": 1200,
                        "number": "1200",
                        "site": "main-site",
                        "status": "new",
                        "phone": "+79991112233",
                        "delivery": {"cost": 0, "address": {"text": "Москва"}},
                        "items": [
                            {
                                "id": 1,
                                "initialPrice": 100,
                                "discountTotal": 0,
                                "quantity": 1,
                                "offer": {"id": 1, "name": "Товар", "vatRate": "10"},
                            }
                        ],
                        "customFields": {},
                    },
                },
            )
        if request.url.path == "/api/v5/orders/1200/edit":
            return httpx.Response(200, json={"success": True, "id": 1200})
        return httpx.Response(404, json={"success": False})

    def komtet_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/shop/v2/queues/12345":
            return httpx.Response(200, json={"id": 12345, "state": "active"})
        if request.url.path == "/api/shop/v2/employees":
            return httpx.Response(
                200, json={"account_employees": [{"id": 7389, "name": "Семенов", "type": "cashier"}]}
            )
        if request.url.path == "/api/shop/v2/orders" and request.method == "GET":
            return httpx.Response(
                403,
                json={
                    "title": "Нет активной услуги",
                    "code": "SRV00",
                    "description": "Необходимо активировать услугу",
                },
            )
        if request.url.path == "/api/shop/v2/orders" and request.method == "POST":
            return httpx.Response(200, json={"id": 5550, "external_id": "1200", "state": "new"})
        if request.url.path == "/api/shop/v2/orders/5550" and request.method == "PUT":
            return httpx.Response(200, json={"id": 5550, "external_id": "1200", "state": "new"})
        return httpx.Response(404)

    def make_crm(cfg: RuntimeConfig) -> RetailCRMClient:
        return RetailCRMClient(
            cfg.retailcrm_url,
            cfg.retailcrm_api_key,
            client=httpx.AsyncClient(transport=httpx.MockTransport(crm_handler)),
        )

    def make_komtet(cfg: RuntimeConfig) -> KomtetClient:
        return KomtetClient(
            cfg.komtet_shop_id,
            cfg.komtet_secret_key,
            api_version=cfg.komtet_api_version,
            client=httpx.AsyncClient(transport=httpx.MockTransport(komtet_handler)),
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[admin_api.get_retailcrm_factory] = lambda: make_crm
    app.dependency_overrides[admin_api.get_komtet_factory] = lambda: make_komtet
    app.dependency_overrides[webhooks.get_retailcrm_factory] = lambda: make_crm
    app.dependency_overrides[webhooks.get_komtet_factory] = lambda: make_komtet

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1) Check seeded settings with masked secrets
        r_set = await client.get("/api/v1/settings")
        assert r_set.status_code == 200
        s_data = r_set.json()
        assert s_data["tax_system"] == 0
        assert s_data["final_status"] == "complete"
        assert s_data["retailcrm_api_key_masked"] == "****9999"
        assert s_data["komtet_secret_key_masked"] == "****8888"

        # Update settings while sending masked secret -> real secret must NOT be overwritten
        r_put = await client.put(
            "/api/v1/settings",
            json={
                "retailcrm_api_key": "****9999",
                "default_vat": "10",
                "anti_loop_seconds": 20,
            },
        )
        assert r_put.status_code == 200
        assert r_put.json()["default_vat"] == "10"
        assert r_put.json()["anti_loop_seconds"] == 20
        async with session_maker() as check_db:
            cfg = await load_runtime_config(check_db, env_settings=test_settings)
            assert cfg.retailcrm_api_key == "test-crm-api-key-9999"

        # 2) Test connection & custom field check (with graceful SRV00 warning)
        r_conn = await client.post("/api/v1/settings/test-connection")
        assert r_conn.status_code == 200
        conn_data = r_conn.json()
        assert conn_data["retailcrm_ok"] is True
        assert conn_data["komtet_field_exists"] is True
        assert conn_data["komtet_ok"] is True
        assert conn_data["komtet_queue_active"] is True
        assert conn_data["komtet_delivery_service_active"] is False
        assert any("SRV00" in w for w in conn_data["warnings"])

        # 3) Live dictionaries graceful degradation
        r_dict = await client.get("/api/v1/dictionaries")
        assert r_dict.status_code == 200
        dict_data = r_dict.json()
        assert len(dict_data["retailcrm_statuses"]) == 1
        assert len(dict_data["komtet_employees"]) == 1
        assert any("SRV00" in w for w in dict_data["warnings"])

        # 4) Seeded mappings & bulk update
        r_map = await client.get("/api/v1/mappings")
        assert r_map.status_code == 200
        assert len(r_map.json()["items"]) >= 8

        r_map_put = await client.put(
            "/api/v1/mappings",
            json={
                "items": [
                    {
                        "mapping_type": "courier",
                        "retailcrm_code": "1",
                        "retailcrm_name": "Александр",
                        "komtet_code": "701",
                        "komtet_name": "Курьер 1",
                        "is_active": True,
                    }
                ],
                "replace_types": ["courier"],
            },
        )
        assert r_map_put.status_code == 200
        assert any(
            i["mapping_type"] == "courier" and i["komtet_code"] == "701"
            for i in r_map_put.json()["items"]
        )

        # 5) RetailCRM form-urlencoded webhook + manual sync + logs
        r_wh = await client.post(
            "/webhooks/retailcrm",
            data={"order_id": "1200", "event": "order_create"},
        )
        assert r_wh.status_code == 200
        assert r_wh.json()["result"]["status"] == "created"
        assert r_wh.json()["result"]["komtet_order_id"] == 5550

        r_manual = await client.post("/api/v1/sync/manual/1200")
        assert r_manual.status_code == 200
        assert r_manual.json()["result"]["status"] == "updated"

        # 6) KOMTET webhook signature check via HTTP endpoint
        bad_cb = await client.post(
            "/webhooks/komtet",
            json={"id": 5550, "external_id": "1200", "state": "new"},
            headers={"X-HMAC-Signature": "bad"},
        )
        assert bad_cb.status_code == 401

        good_payload = {"id": 5550, "external_id": "1200", "state": "new"}
        good_raw = json.dumps(good_payload)
        good_sig = KomtetClient.compute_signature(
            "test-komtet-secret-8888",
            "POST",
            "http://testserver/webhooks/komtet",
            good_raw,
        )
        good_cb = await client.post(
            "/webhooks/komtet",
            content=good_raw,
            headers={
                "Content-Type": "application/json",
                "X-HMAC-Signature": good_sig,
            },
        )
        assert good_cb.status_code == 200
        assert good_cb.json()["result"]["status"] == "ignored"

        # 7) Logs & orders-sync endpoints
        r_logs = await client.get("/api/v1/logs?order_id=1200&limit=10")
        assert r_logs.status_code == 200
        assert r_logs.json()["total"] >= 3

        r_err_logs = await client.get("/api/v1/logs?status=ERROR")
        assert r_err_logs.status_code == 200
        assert r_err_logs.json()["total"] >= 1

        r_succ_logs = await client.get(
            "/api/v1/logs?status=success&direction=retailcrm_to_komtet"
        )
        assert r_succ_logs.status_code == 200
        assert r_succ_logs.json()["total"] >= 2

        r_skip_logs = await client.get(
            "/api/v1/logs?status=skipped&direction=komtet_to_retailcrm"
        )
        assert r_skip_logs.status_code == 200
        assert r_skip_logs.json()["total"] >= 1

        r_orders = await client.get("/api/v1/orders-sync")
        assert r_orders.status_code == 200
        assert r_orders.json()["total"] == 1

    await engine.dispose()


# ---------------------------------------------------------------------------
# 4. Audit Regression Tests (CRITICAL, HIGH & MEDIUM fixes)
# ---------------------------------------------------------------------------


def test_komtet_v2_item_ids_split_and_no_internal_id_collision() -> None:
    """[CRITICAL #3] v2 items use only external_id (with _split suffix) and never match KOMTET id."""
    crm_order = {
        "id": 6001,
        "status": "send-to-delivery",
        "phone": "+79991112233",
        "summ": 290.00,
        "delivery": {"cost": 350.00, "address": {"text": "Москва, ул. Мира 1"}},
        "items": [
            {
                "id": 101,
                "initialPrice": 100.00,
                "discountTotal": 0,
                "quantity": 3,
                "offer": {"id": 501, "name": "Поводок", "vatRate": "10"},
            },
            {
                "id": 102,
                "initialPrice": 500.00,
                "discountTotal": 0,
                "quantity": 1,
                "offer": {"id": 502, "name": "Миска", "vatRate": "10"},
            },
        ],
    }

    # Verify outbound v2 items have no `id` and distinct `external_id`s
    crm_order_single = {**crm_order, "items": [crm_order["items"][0]]}
    payload, _ = map_retailcrm_order_to_komtet(
        crm_order_single, _sample_config(komtet_api_version="v2"), _sample_mappings()
    )
    assert all("id" not in item for item in payload["items"])
    assert [item["external_id"] for item in payload["items"]] == ["101", "101_split", "delivery"]

    # Reconcile split lines by external_id (`101` + `101_split` -> 2 units of CRM item 101)
    changed, patch = _reconcile_partial_redemption(
        crm_order,
        [
            {"id": 9001, "external_id": "101", "name": "Поводок", "quantity": 1, "total": 96.66, "type": "product"},
            {"id": 9002, "external_id": "101_split", "name": "Поводок", "quantity": 1, "total": 96.67, "type": "product"},
            {"id": 9003, "external_id": "delivery", "name": "Доставка", "quantity": 1, "total": 350.0, "type": "service"},
        ],
    )
    assert changed is True
    assert len(patch["items"]) == 1
    assert patch["items"][0]["id"] == 101
    assert patch["items"][0]["quantity"] == 2

    # Callback WITHOUT external_id where KOMTET's internal `id` (102) happens to equal another CRM item's id:
    # Must NEVER match item 102 by `id`; must fall back to name ("Поводок" -> CRM item 101).
    changed_name, patch_name = _reconcile_partial_redemption(
        crm_order,
        [
            {"id": 102, "name": "Поводок", "quantity": 1, "total": 96.66, "type": "product"},
            {"id": 9999, "name": "Доставка", "quantity": 1, "total": 350.0, "type": "service"},
        ],
    )
    assert changed_name is True
    assert len(patch_name["items"]) == 1
    assert patch_name["items"][0]["id"] == 101
    assert patch_name["items"][0]["quantity"] == 1


@pytest.mark.asyncio
async def test_url_validation_and_komtet_no_redirect_on_post_put(tmp_path: Path) -> None:
    """[CRITICAL #4] SSRF/scheme validation on settings URLs and no redirects on KOMTET POST/PUT."""
    assert validate_outbound_url("https://shop.retailcrm.ru/") == "https://shop.retailcrm.ru"
    assert validate_outbound_url("http://shop.retailcrm.ru", allow_http=True) == "http://shop.retailcrm.ru"

    forbidden_urls = [
        "http://shop.retailcrm.ru",
        "ftp://shop.retailcrm.ru",
        "https://localhost/api",
        "https://127.0.0.1/api",
        "https://10.0.0.1/api",
        "https://172.16.5.1/api",
        "https://192.168.1.10/api",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/api",
    ]
    for bad_url in forbidden_urls:
        with pytest.raises(ValueError):
            validate_outbound_url(bad_url, allow_http=False)

    # Admin API PUT /api/v1/settings rejects SSRF / http URLs in non-test environments with 422
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'sec.db').as_posix()}"
    engine = create_engine(db_url)
    prod_like_settings = _test_env_settings(app_env="development")
    await init_db(engine, seed=True, env_settings=prod_like_settings)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(prod_like_settings)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r_bad_ip = await client.put("/api/v1/settings", json={"retailcrm_url": "https://127.0.0.1"})
        assert r_bad_ip.status_code == 422
        r_bad_http = await client.put("/api/v1/settings", json={"komtet_base_url": "http://kassa.komtet.ru"})
        assert r_bad_http.status_code == 422
    await engine.dispose()

    # KomtetClient must NOT follow redirects on POST/PUT
    seen_urls: list[str] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.host == "kassa.komtet.ru":
            return httpx.Response(307, headers={"Location": "https://evil.example.com/steal"})
        return httpx.Response(200, json={"id": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler)) as http_client:
        komtet = KomtetClient(
            "test-shop", "test-secret", client=http_client, retry_delays=()
        )
        with pytest.raises(KomtetError) as exc_post:
            await komtet.create_order({"external_id": "1"})
        assert exc_post.value.status_code == 307

        with pytest.raises(KomtetError) as exc_put:
            await komtet.update_order(1, {"external_id": "1"})
        assert exc_put.value.status_code == 307

    assert all("evil.example.com" not in u for u in seen_urls)


def test_partial_redemption_full_refusal_preserve_fields_and_order_discount() -> None:
    """[HIGH #5] Full goods refusal, field preservation, delivery.code, and order discount recalc."""
    # 1) Full refusal of all goods, client pays only delivery
    crm_order_refusal = {
        "id": 7001,
        "discountManualAmount": 200.0,
        "delivery": {"code": "courier-msk", "cost": 350.0},
        "items": [
            {
                "id": 11,
                "initialPrice": 1000.0,
                "quantity": 1,
                "offer": {"id": 501, "name": "Корм"},
            }
        ],
    }
    changed, patch = _reconcile_partial_redemption(
        crm_order_refusal,
        [
            {"external_id": "delivery", "name": "Доставка", "quantity": 1, "total": 350.0, "type": "service"},
        ],
    )
    assert changed is True
    assert patch["items"] == []
    assert patch["discountManualAmount"] == 0

    # 2) Partial redemption with order-level discount + extra item fields + delivery removal
    crm_order_disc = {
        "id": 7002,
        "discountManualAmount": 500.0,
        "delivery": {"code": "courier-msk", "cost": 300.0},
        "items": [
            {
                "id": 21,
                "initialPrice": 1000.0,
                "purchasePrice": 600.0,
                "productName": "Лежанка большая",
                "vatRate": "20",
                "properties": [{"code": "color", "name": "Цвет", "value": "Серый"}],
                "discountManualAmount": 0,
                "discountManualPercent": 0,
                "quantity": 1,
                "offer": {"id": 601, "name": "Лежанка большая"},
            },
            {
                "id": 22,
                "initialPrice": 1000.0,
                "quantity": 1,
                "offer": {"id": 602, "name": "Когтеточка"},
            },
        ],
    }
    # Client buys only item 21 at 750.00 (500 order discount split 250/250), delivery removed
    changed_d, patch_d = _reconcile_partial_redemption(
        crm_order_disc,
        [
            {"external_id": "21", "name": "Лежанка большая", "quantity": 1, "total": 750.0, "type": "product"},
        ],
    )
    assert changed_d is True
    assert patch_d["discountManualAmount"] == 250.0
    assert patch_d["delivery"] == {"cost": 0, "code": "courier-msk"}
    kept = patch_d["items"][0]
    assert kept["id"] == 21
    assert kept["offer"] == {"id": 601}
    assert kept["initialPrice"] == 1000.0
    assert kept["purchasePrice"] == 600.0
    assert kept["productName"] == "Лежанка большая"
    assert kept["vatRate"] == "20"
    assert kept["properties"] == [{"code": "color", "name": "Цвет", "value": "Серый"}]


def test_mapper_valid_payment_type_prices_array_and_penny_reconciliation() -> None:
    """[HIGH #6, #7] payment_type is never 'prepayment'; prices[] & upward penny reconcile work."""
    # 1) Fully prepaid order via `oplachen` -> payment_type must be 'card', never 'prepayment'
    prepaid_order = {
        "id": 8001,
        "phone": "+79990001122",
        "summ": 1000.0,
        "delivery": {"cost": 0, "address": {"text": "Москва"}},
        "items": [{"id": 1, "initialPrice": 1000.0, "quantity": 1, "offer": {"name": "Корм"}}],
        "payments": {"1": {"id": 1, "type": "oplachen", "status": "paid", "amount": 1000.0}},
    }
    payload_v2, _ = map_retailcrm_order_to_komtet(
        prepaid_order, _sample_config(komtet_api_version="v2"), _sample_mappings()
    )
    assert payload_v2["is_pay_to_courier"] is False
    assert payload_v2["prepayment"] == 1000.0
    assert payload_v2["payment_type"] == "card"

    payload_v1, _ = map_retailcrm_order_to_komtet(
        prepaid_order, _sample_config(komtet_api_version="v1"), _sample_mappings()
    )
    assert payload_v1["is_paid"] is True
    assert payload_v1["payment_type"] == "card"

    # 2) Partial prepayment via `oplachen` without a second unpaid payment -> still valid 'card'
    partial_prepaid = {
        **prepaid_order,
        "payments": {"1": {"id": 1, "type": "oplachen", "status": "paid", "amount": 200.0}},
    }
    payload_part, _ = map_retailcrm_order_to_komtet(
        partial_prepaid, _sample_config(komtet_api_version="v2"), _sample_mappings()
    )
    assert payload_part["is_pay_to_courier"] is True
    assert payload_part["prepayment"] == 200.0
    assert payload_part["payment_type"] == "card"

    # 3) item["prices"] exact breakdown used without double-subtracting order.discountManualAmount
    prices_order = {
        "id": 8002,
        "phone": "+79990001122",
        "discountManualAmount": 10.0,  # order.summ omitted on purpose!
        "delivery": {"cost": 0, "address": {"text": "Москва"}},
        "items": [
            {
                "id": 10,
                "initialPrice": 100.0,
                "discountTotal": 3.33,
                "quantity": 3,
                "prices": [
                    {"price": 96.67, "quantity": 2},
                    {"price": 96.66, "quantity": 1},
                ],
                "offer": {"name": "Корм"},
            }
        ],
    }
    p_prices, _ = map_retailcrm_order_to_komtet(
        prices_order, _sample_config(komtet_api_version="v2"), _sample_mappings()
    )
    assert sum(Decimal(str(i["total"])) for i in p_prices["items"]) == Decimal("290.00")

    # 4) No double discount when order.summ is absent and item.discountTotal is already populated
    no_summ_order = {
        "id": 8003,
        "phone": "+79990001122",
        "discountManualAmount": 100.0,
        "delivery": {"cost": 0, "address": {"text": "Москва"}},
        "items": [
            {
                "id": 11,
                "initialPrice": 500.0,
                "discountTotal": 50.0,  # 50 * 2 = 100 already baked in!
                "quantity": 2,
                "offer": {"name": "Игрушка"},
            }
        ],
    }
    p_nodbl, _ = map_retailcrm_order_to_komtet(
        no_summ_order, _sample_config(komtet_api_version="v2"), _sample_mappings()
    )
    assert sum(Decimal(str(i["total"])) for i in p_nodbl["items"]) == Decimal("900.00")

    # 5) Upward penny reconciliation when rounded discountTotal gives items_sum < order.summ
    upward_order = {
        "id": 8004,
        "phone": "+79990001122",
        "summ": 290.00,
        "delivery": {"cost": 0, "address": {"text": "Москва"}},
        "items": [
            {
                "id": 12,
                "initialPrice": 100.0,
                "discountTotal": 3.34,  # (100 - 3.34) * 3 = 289.98 < 290.00
                "quantity": 3,
                "offer": {"name": "Лакомство"},
            }
        ],
    }
    p_up, _ = map_retailcrm_order_to_komtet(
        upward_order, _sample_config(komtet_api_version="v2"), _sample_mappings()
    )
    assert sum(Decimal(str(i["total"])) for i in p_up["items"]) == Decimal("290.00")
    for line in p_up["items"]:
        assert Decimal(str(line["price"])) * Decimal(str(line["quantity"])) == Decimal(str(line["total"]))


@pytest.mark.asyncio
async def test_clients_retry_on_429_503_and_transport_error() -> None:
    """[HIGH #12] RetailCRMClient and KomtetClient retry on 429, 503, and TransportError."""
    crm_attempts = 0

    def crm_flaky(request: httpx.Request) -> httpx.Response:
        nonlocal crm_attempts
        crm_attempts += 1
        if crm_attempts == 1:
            raise httpx.ConnectError("transient network drop", request=request)
        if crm_attempts == 2:
            return httpx.Response(429, json={"success": False, "errorMsg": "Rate limit"})
        if crm_attempts == 3:
            return httpx.Response(503, json={"success": False, "errorMsg": "Unavailable"})
        return httpx.Response(200, json={"success": True, "order": {"id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(crm_flaky)) as http_client:
        crm = RetailCRMClient(
            "https://example.retailcrm.ru",
            "test-key",
            retry_delays=(0.0, 0.0, 0.0),
            client=http_client,
        )
        res = await crm.get_order(42, site="main-site")
        assert res["order"]["id"] == 42
        assert crm_attempts == 4

    komtet_attempts = 0

    def komtet_flaky(request: httpx.Request) -> httpx.Response:
        nonlocal komtet_attempts
        komtet_attempts += 1
        if komtet_attempts == 1:
            return httpx.Response(502, json={"title": "Bad gateway"})
        if komtet_attempts == 2:
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(200, json={"id": 99, "state": "new"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(komtet_flaky)) as http_client:
        komtet = KomtetClient(
            "test-shop",
            "test-secret",
            retry_delays=(0.0, 0.0, 0.0),
            client=http_client,
        )
        res = await komtet.create_order({"external_id": "42"})
        assert res["id"] == 99
        assert komtet_attempts == 3


@pytest.mark.asyncio
async def test_idempotency_prepay_sum_done_guard_resolve_link_and_rollback(tmp_path: Path) -> None:
    """[HIGH #9, #10, #11, #17] externalId, prepaySum, MSK paidAt, done guard, link recovery, rollback."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'audit_sync.db').as_posix()}"
    engine = create_engine(db_url)
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    created_payments: list[dict[str, Any]] = []
    crm_get_sites: list[str | None] = []
    crm_fail_mode = {"fail_503": False}
    komtet_calls: list[tuple[str, str]] = []
    komtet_get_payload: dict[str, Any] = {"id": 5000, "external_id": "910", "state": "done"}

    def crm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/orders/910" and request.method == "GET":
            crm_get_sites.append(request.url.params.get("site"))
            if crm_fail_mode["fail_503"]:
                return httpx.Response(503, json={"success": False, "errorMsg": "CRM down"})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "order": {
                        "id": 910,
                        "number": "910",
                        "site": "main-site",
                        "status": "cancel-other",
                        "totalSumm": 1000.0,
                        "prepaySum": 400.0,  # prepayment recorded in prepaySum without payments entry
                        "delivery": {"cost": 0},
                        "items": [
                            {"id": 1, "initialPrice": 1000.0, "quantity": 1, "offer": {"id": 1, "name": "Корм"}}
                        ],
                        "payments": {},
                        "customFields": {"komtet_id": 5000},
                    },
                },
            )
        if request.url.path == "/api/v5/orders/910/edit":
            return httpx.Response(200, json={"success": True, "id": 910})
        if request.url.path == "/api/v5/orders/payments/create":
            form = parse_qs(request.content.decode("utf-8"))
            created_payments.append(json.loads(form["payment"][0]))
            return httpx.Response(201, json={"success": True, "id": 701})
        if request.url.path == "/api/v5/orders/911" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "order": {
                        "id": 911,
                        "status": "send-to-delivery",
                        "site": "main-site",
                        "phone": "+79991112233",
                        "summ": 500.0,
                        "delivery": {"cost": 0, "address": {"text": "Москва"}},
                        "items": [{"id": 2, "initialPrice": 500.0, "quantity": 1, "offer": {"id": 2, "name": "Миска"}}],
                        "customFields": {"komtet_id": 5001},
                    },
                },
            )
        if request.url.path == "/api/v5/orders/911/edit":
            return httpx.Response(200, json={"success": True, "id": 911})
        return httpx.Response(404, json={"success": False})

    def komtet_handler(request: httpx.Request) -> httpx.Response:
        komtet_calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/shop/v2/orders/5000":
            return httpx.Response(200, json=komtet_get_payload)
        if request.method == "GET" and request.url.path == "/api/shop/v2/orders/5001":
            # Missing external_id/order_id -> must NOT be adopted!
            return httpx.Response(200, json={"id": 5001, "state": "new"})
        if request.method == "POST" and request.url.path == "/api/shop/v2/orders":
            return httpx.Response(200, json={"id": 5002, "external_id": "911", "state": "new"})
        return httpx.Response(200, json={})

    async with (
        session_maker() as db,
        httpx.AsyncClient(transport=httpx.MockTransport(crm_handler)) as crm_http,
        httpx.AsyncClient(transport=httpx.MockTransport(komtet_handler)) as k_http,
    ):
        crm_cli = RetailCRMClient(
            "https://example.retailcrm.ru", "k", retry_delays=(), client=crm_http
        )
        komtet_cli = KomtetClient(
            "test-shop-id", "test-komtet-secret-8888", retry_delays=(), client=k_http
        )

        # 1) Rollback check: transient 503 during new callback must rollback (no lingering pending OrderSync)
        crm_fail_mode["fail_503"] = True
        cb = {
            "id": 5000,
            "external_id": "910",
            "state": "done",
            "payment_type": "card",
            "amount": 1000.0,
            "items": [{"external_id": "1", "name": "Корм", "quantity": 1, "total": 1000.0, "type": "product"}],
        }
        raw_cb = json.dumps(cb).encode("utf-8")
        sig = KomtetClient.compute_signature(
            "test-komtet-secret-8888",
            "POST",
            "https://sync.example.com/webhooks/komtet",
            raw_cb.decode("utf-8"),
        )
        err_res = await sync_komtet_to_retailcrm(
            db,
            cb,
            signature=sig,
            raw_url="https://sync.example.com/webhooks/komtet",
            raw_body=raw_cb,
            crm_client=crm_cli,
        )
        assert err_res["status"] == "error"
        assert err_res["transient_error"] is True
        # No uncommitted `pending` row should have been persisted after rollback
        lingering = await db.scalar(select(OrderSync).where(OrderSync.retailcrm_order_id == 910))
        assert lingering is None
        # Default site was sent on first crm.get_order
        assert crm_get_sites[-1] == "main-site"

        # 2) Successful callback: prepaySum=400 reduces remaining_to_pay to 600, sets externalId and MSK paidAt
        crm_fail_mode["fail_503"] = False
        msk_before = datetime.now(get_app_timezone("Europe/Moscow")).replace(microsecond=0)
        ok_res = await sync_komtet_to_retailcrm(
            db,
            cb,
            signature=sig,
            raw_url="https://sync.example.com/webhooks/komtet",
            raw_body=raw_cb,
            crm_client=crm_cli,
        )
        msk_after = datetime.now(get_app_timezone("Europe/Moscow")).replace(microsecond=0)
        assert ok_res["status"] == "processed"
        assert ok_res["payment_amount"] == 600.0
        assert len(created_payments) == 1
        assert created_payments[0]["externalId"] == "komtet-5000"
        assert created_payments[0]["amount"] == 600.0
        paid_dt = datetime.strptime(created_payments[0]["paidAt"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=get_app_timezone("Europe/Moscow")
        )
        assert msk_before <= paid_dt <= msk_after + timedelta(seconds=2)

        # 3) Already `done` order moved to cancel status in CRM -> must NOT call DELETE in KOMTET
        komtet_calls.clear()
        osync = await db.scalar(select(OrderSync).where(OrderSync.retailcrm_order_id == 910))
        assert osync is not None
        osync.last_komtet_callback_at = None  # expire anti-loop window explicitly
        await db.commit()

        skip_done = await sync_retailcrm_to_komtet(
            db, 910, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert skip_done["status"] == "skipped"
        assert skip_done["reason"] == "already_done"
        assert not any(m == "DELETE" for m, _ in komtet_calls)

        # 4) Recovery from customFields when DB row is deleted and remote KOMTET state is `done`:
        # Must mark completed and NEVER issue PUT/DELETE
        await db.delete(osync)
        await db.commit()
        komtet_calls.clear()
        rec_done = await sync_retailcrm_to_komtet(
            db, 910, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert rec_done["status"] == "skipped"
        assert rec_done["reason"] == "already_done"
        assert komtet_calls == [("GET", "/api/shop/v2/orders/5000")]
        recovered = await db.scalar(select(OrderSync).where(OrderSync.retailcrm_order_id == 910))
        assert recovered is not None
        assert recovered.komtet_state == "done"
        assert recovered.sync_status == "completed"

        # 5) Recovery rejects KOMTET order with missing external_id/order_id and creates a new order
        komtet_calls.clear()
        res_911 = await sync_retailcrm_to_komtet(
            db, 911, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res_911["status"] == "created"
        assert res_911["komtet_order_id"] == 5002
        assert ("PUT", "/api/shop/v2/orders/5001") not in komtet_calls

    await engine.dispose()


@pytest.mark.asyncio
async def test_webhooks_http_503_retry_hmac_before_factory_proxy_ports_and_form_json(
    tmp_path: Path,
) -> None:
    """[CRITICAL #2, HIGH #8, MEDIUM #14, #15] 503 on transient, 200 on SRV00, HMAC/proxy, form JSON."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'wh_audit.db').as_posix()}"
    engine = create_engine(db_url)
    test_settings = _test_env_settings()
    await init_db(engine, seed=True, env_settings=test_settings)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    app = create_app(test_settings)
    crm_factory_calls = 0
    seen_crm_sites: list[str | None] = []
    mode = {"crm_503": False, "komtet_srv00": False}

    def crm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/api/v5/orders/1500", "/api/v5/orders/1501"} and request.method == "GET":
            oid = int(request.url.path.rsplit("/", 1)[-1])
            seen_crm_sites.append(request.url.params.get("site"))
            if mode["crm_503"]:
                return httpx.Response(503, json={"success": False, "errorMsg": "CRM unavailable"})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "order": {
                        "id": oid,
                        "site": request.url.params.get("site") or "main-site",
                        "status": "new",
                        "phone": "+79991112233",
                        "delivery": {"cost": 0, "address": {"text": "Москва"}},
                        "items": [
                            {"id": 1, "initialPrice": 100, "quantity": 1, "offer": {"id": 1, "name": "Корм"}}
                        ],
                        "customFields": {},
                    },
                },
            )
        if request.url.path in {"/api/v5/orders/1500/edit", "/api/v5/orders/1501/edit"}:
            oid = int(request.url.path.split("/")[-2])
            return httpx.Response(200, json={"success": True, "id": oid})
        return httpx.Response(404, json={"success": False})

    def komtet_handler(request: httpx.Request) -> httpx.Response:
        if mode["komtet_srv00"]:
            return httpx.Response(
                403,
                json={"title": "Нет услуги", "code": "SRV00", "description": "Активируйте услугу"},
            )
        if request.method == "POST" and request.url.path == "/api/shop/v2/orders":
            req_body = json.loads(request.content.decode("utf-8"))
            new_kid = 6601 if str(req_body.get("external_id")) == "1501" else 6600
            return httpx.Response(
                200, json={"id": new_kid, "external_id": str(req_body.get("external_id")), "state": "new"}
            )
        return httpx.Response(200, json={})

    def make_crm(cfg: RuntimeConfig) -> RetailCRMClient:
        nonlocal crm_factory_calls
        crm_factory_calls += 1
        return RetailCRMClient(
            cfg.retailcrm_url,
            cfg.retailcrm_api_key,
            retry_delays=(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(crm_handler)),
        )

    def make_komtet(cfg: RuntimeConfig) -> KomtetClient:
        return KomtetClient(
            cfg.komtet_shop_id,
            cfg.komtet_secret_key,
            api_version=cfg.komtet_api_version,
            retry_delays=(),
            client=httpx.AsyncClient(transport=httpx.MockTransport(komtet_handler)),
        )

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[webhooks.get_retailcrm_factory] = lambda: make_crm
    app.dependency_overrides[webhooks.get_komtet_factory] = lambda: make_komtet

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1) Invalid KOMTET HMAC -> 401 and crm_factory must NOT be called
        r_bad = await client.post(
            "/webhooks/komtet",
            json={"id": 6600, "external_id": "1500", "state": "done"},
            headers={"X-HMAC-Signature": "wrong"},
        )
        assert r_bad.status_code == 401
        assert crm_factory_calls == 0

        # 2) Reverse proxy with explicit :443 in X-Forwarded-Host + query string -> valid HMAC passes
        cb_body = json.dumps({"id": 6600, "external_id": "1500", "state": "new"})
        sig_proxy = KomtetClient.compute_signature(
            "test-komtet-secret-8888",
            "POST",
            "https://proxy.example.com/webhooks/komtet?src=komtet",
            cb_body,
        )
        r_proxy = await client.post(
            "/webhooks/komtet?src=komtet",
            content=cb_body,
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "proxy.example.com:443",
                "X-HMAC-Signature": sig_proxy,
            },
        )
        assert r_proxy.status_code == 200
        assert r_proxy.json()["result"]["status"] == "ignored"

        # 3) Configured komtet_callback_url + incoming query string (even without X-Forwarded-Proto)
        sig_cfg_query = KomtetClient.compute_signature(
            "test-komtet-secret-8888",
            "POST",
            "https://sync.example.com/webhooks/komtet?v=2",
            cb_body,
        )
        r_cfg_query = await client.post(
            "/webhooks/komtet?v=2",
            content=cb_body,
            headers={
                "Content-Type": "application/json",
                "X-HMAC-Signature": sig_cfg_query,
            },
        )
        assert r_cfg_query.status_code == 200

        # 4) Transient 503 from RetailCRM during /webhooks/komtet -> HTTP 503 for retry
        mode["crm_503"] = True
        done_body = json.dumps({"id": 6600, "external_id": "1500", "state": "done"})
        sig_done = KomtetClient.compute_signature(
            "test-komtet-secret-8888",
            "POST",
            "https://sync.example.com/webhooks/komtet",
            done_body,
        )
        r_503_k = await client.post(
            "/webhooks/komtet",
            content=done_body,
            headers={"Content-Type": "application/json", "X-HMAC-Signature": sig_done},
        )
        assert r_503_k.status_code == 503

        # 5) Transient 503 during /webhooks/retailcrm -> HTTP 503 for retry
        r_503_crm = await client.post("/webhooks/retailcrm", data={"order_id": "1500"})
        assert r_503_crm.status_code == 503

        # 6) Business error (403 SRV00) during /webhooks/retailcrm -> HTTP 200 (no endless retries)
        mode["crm_503"] = False
        mode["komtet_srv00"] = True
        r_srv00 = await client.post("/webhooks/retailcrm", data={"order_id": "1500"})
        assert r_srv00.status_code == 200
        assert r_srv00.json()["result"]["status"] == "error"
        assert r_srv00.json()["result"]["transient_error"] is False

        # 7) RetailCRM webhook with form-urlencoded JSON string in `order` + `event` in query + `site`
        mode["komtet_srv00"] = False
        seen_crm_sites.clear()
        r_form_json = await client.post(
            "/webhooks/retailcrm?event=order_edit",
            data={"order": json.dumps({"id": 1501, "site": "shop-alt"})},
        )
        assert r_form_json.status_code == 200
        assert r_form_json.json()["result"]["status"] == "created"
        assert seen_crm_sites == ["shop-alt"]

        async with session_maker() as check_db:
            log_row = await check_db.scalar(
                select(SyncLog)
                .where(SyncLog.retailcrm_order_id == 1501, SyncLog.event_type == "order_create")
                .order_by(SyncLog.id.desc())
            )
            assert log_row is not None
            assert log_row.payload["trigger_event"] == "order_edit"

    await engine.dispose()
