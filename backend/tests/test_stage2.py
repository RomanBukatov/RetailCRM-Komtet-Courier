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
    _distribute_discount,
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
    run_reconciliation_cycle,
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
        "admin_api_token": "",
        "retailcrm_webhook_token": "",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


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


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize(("order_email", "customer_email", "expected"), [
    ("", " ", "example@example.com"),
    ("invalid", "invalid@domain", "example@example.com"),
    ("invalid", " buyer@example.org ", "buyer@example.org"),
    ("order@example.org", "buyer@example.org", "order@example.org"),
])
def test_mapper_fallback_client_email(version, order_email, customer_email, expected) -> None:
    order = {
        "id": 42, "email": order_email, "customer": {"email": customer_email},
        "items": [{"id": 10, "initialPrice": 100, "quantity": 1, "productName": "Корм"}],
    }
    cfg = _sample_config(komtet_api_version=version, fallback_client_email=" example@example.com ")
    payload, _ = map_retailcrm_order_to_komtet(order, cfg, _sample_mappings())
    assert (payload["client_email"] if version == "v1" else payload["client"]["email"]) == expected


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


@pytest.mark.parametrize(
    ("marked", "order_sum", "expected"),
    [
        (True, 1350, [("473148_m1", 1, 450, 450), ("473148_m2", 1, 450, 450),
                      ("473148_m3", 1, 450, 450)]),
        (True, 100, [("473148_m1", 1, 33.34, 33.34), ("473148_m2", 1, 33.33, 33.33),
                     ("473148_m3", 1, 33.33, 33.33)]),
        (False, 1350, [("473148", 3, 450, 1350)]),
    ],
)
def test_mapper_v2_marked_units(marked, order_sum, expected) -> None:
    order = {
        "id": 315568,
        "summ": order_sum,
        "items": [{"id": 473148, "quantity": 3, "initialPrice": 450}],
    }
    config = _sample_config(komtet_api_version="v2", require_mark_code=marked)
    payload, _ = map_retailcrm_order_to_komtet(order, config, _sample_mappings())
    items = payload["items"]
    assert [
        (item["external_id"], item["quantity"], item["price"], item["total"])
        for item in items
    ] == expected
    assert all(item["is_need_mark_code"] is marked for item in items)
    assert sum(Decimal(str(item["total"])) for item in items) == Decimal(order_sum)


def test_mapper_rejects_marked_quantity_above_limit() -> None:
    order = {"id": 315568, "items": [{"id": 473148, "quantity": 501, "initialPrice": 450}]}
    config = _sample_config(komtet_api_version="v2", require_mark_code=True)
    with pytest.raises(ValueError, match="473148 exceeds the 500-unit limit"):
        map_retailcrm_order_to_komtet(order, config, _sample_mappings())


@pytest.mark.parametrize("require_mark_code", [False, True])
def test_mapper_discounts_penny_exact_split_delivery_and_prepayment(require_mark_code: bool) -> None:
    """Order discount creating odd cents per unit must split into exact price*quantity==total lines."""
    crm_order = {
        "id": 5001,
        "number": "31553401",
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
        _sample_config(komtet_api_version="v2", require_mark_code=require_mark_code),
        _sample_mappings(),
    )

    assert payload["external_id"] == "31553401"
    assert payload["company"]["sno"] == 0
    assert payload["courier_id"] == 701
    assert payload["client"]["phone"] == "+79161112233"
    assert payload["date_start"] == "21.11.2026 10:00"
    assert payload["date_end"] == "21.11.2026 14:00"
    assert payload["prepayment"] == 200.0
    assert payload["is_pay_to_courier"] is True
    assert payload["payment_type"] == "card"
    assert len(payload_hash) == 64

    items = payload["items"]
    assert len(items) == (4 if require_mark_code else 3)
    for item in items:
        assert Decimal(str(item["price"])) * Decimal(str(item["quantity"])) == Decimal(
            str(item["total"])
        )

    product_lines = [i for i in items if i["type"] == "product"]
    assert sum(Decimal(str(i["total"])) for i in product_lines) == Decimal("290.00")
    assert all(i["vat"] == "10" for i in product_lines)
    expected_ids = ["91_m1", "91_m2", "91_m3"] if require_mark_code else ["91", "91_split"]
    assert [i["external_id"] for i in product_lines] == expected_ids
    assert all("id" not in i for i in items)

    delivery_lines = [i for i in items if i["type"] == "service"]
    assert len(delivery_lines) == 1
    assert delivery_lines[0]["external_id"] == "delivery"
    assert delivery_lines[0].get("is_need_mark_code", False) is False
    assert all(i["is_need_mark_code"] is require_mark_code for i in product_lines)
    assert all("is_need_nomenclature_code" not in i for i in items)
    assert delivery_lines[0]["name"] == "Доставка"
    assert delivery_lines[0]["total"] == 350.0
    assert delivery_lines[0]["vat"] == "22"

    # Also verify KOMTET v1 payload format
    payload_v1, _ = map_retailcrm_order_to_komtet(
        crm_order,
        _sample_config(komtet_api_version="v1"),
        _sample_mappings(),
    )
    assert payload_v1["order_id"] == "31553401"
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

        # The manager saves an open card and clears the integration field.
        assert len(crm_state["edits"]) == 1
        crm_state["order"]["customFields"]["komtet_id"] = None
        crm_state["edits"].clear()
        komtet_calls.clear()

        # 2) Same payload again -> skipped due to identical SHA-256 hash
        res2 = await sync_retailcrm_to_komtet(
            db, 777, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res2["status"] == "skipped"
        assert res2["reason"] == "unchanged"
        assert res2["komtet_order_id"] == 8881
        assert not crm_state["edits"]
        assert not komtet_calls

        # 3) Order changed (e.g., comment updated) -> update_order called
        crm_state["order"]["customerComment"] = "Позвонить за час"
        res3 = await sync_retailcrm_to_komtet(
            db, 777, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res3["status"] == "updated"
        assert [(method, path) for method, path, _ in komtet_calls] == [
            ("PUT", "/api/shop/v2/orders/8881")
        ]
        assert not crm_state["edits"]

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
@pytest.mark.parametrize("local_link", [True, False])
async def test_reverse_sync_partial_redemption_payment_idempotency_and_antiloop(
    tmp_path: Path, local_link: bool,
) -> None:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'reverse.db').as_posix()}"
    engine = create_engine(db_url)
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    crm_order = {
        "id": 315534,
        "number": "31553401",
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
        if request.url.path == "/api/v5/orders":
            assert not local_link
            assert request.url.params["filter[numbers][]"] == "31553401"
            return httpx.Response(200, json={"orders": [{"id": 315534, "number": "31553401"}]})
        if request.url.path == "/api/v5/orders/315534" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "order": crm_order})
        if request.url.path == "/api/v5/orders/315534/edit" and request.method == "POST":
            form = parse_qs(request.content.decode("utf-8"))
            patch = json.loads(form["order"][0])
            crm_calls.append(("edit_order", request.url.path, patch))
            if "items" in patch:
                crm_order["items"] = patch["items"]
                crm_order["totalSumm"] = 500.0
            if "status" in patch:
                crm_order["status"] = patch["status"]
            return httpx.Response(200, json={"success": True, "id": 315534})
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
        "external_id": "31553401",
        "state": "done",
        "payments": [{"type": "card", "sum": 500.0}],
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
        if local_link:
            db.add(OrderSync(retailcrm_order_id=315534, komtet_external_id="31553401"))
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
        assert crm_calls[1][2]["order"] == {"id": 315534}
        assert crm_calls[1][2]["amount"] == 500.0
        assert crm_calls[2] == ("edit_order", "/api/v5/orders/315534/edit", {"status": "complete"})

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
            315534,
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
            "payments": [{"type": "cash", "sum": 500.0}],
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
        # Point order 901 to same mock handler via 315534 path? Use 315534 with reset OrderSync for clean test:
        osync = await db.scalar(select(OrderSync).where(OrderSync.retailcrm_order_id == 315534))
        assert osync is not None
        osync.last_komtet_callback_hash = None
        await db.commit()
        cash_cb["id"] = 4455
        cash_cb["external_id"] = "315534"
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
        if request.url.path == "/api/v5/reference/delivery-types":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "deliveryTypes": {
                        "courier": {"code": "courier", "name": "Курьер", "active": True},
                        "self-delivery": {"code": "self-delivery", "name": "Самовывоз", "active": True},
                    },
                },
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
        assert len(dict_data["retailcrm_delivery_types"]) == 2
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
        res = await komtet.get_order(99)
        assert res["id"] == 99
        assert komtet_attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["910", "91001"])
async def test_idempotency_prepay_sum_done_guard_resolve_link_and_rollback(tmp_path: Path, owner: str) -> None:
    """[HIGH #9, #10, #11, #17] externalId, prepaySum, MSK paidAt, done guard, link recovery, rollback."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'audit_sync.db').as_posix()}"
    engine = create_engine(db_url)
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    created_payments: list[dict[str, Any]] = []
    crm_get_sites: list[str | None] = []
    crm_fail_mode = {"fail_503": False}
    komtet_calls: list[tuple[str, str]] = []
    komtet_get_payload: dict[str, Any] = {"id": 5000, "external_id": owner, "state": "done"}

    def crm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v5/orders" and request.method == "GET":
            assert request.url.params["filter[numbers][]"] == "910"
            return httpx.Response(200, json={"orders": []})
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
                        "number": "91001",
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
            "payments": [{"type": "card", "sum": 600}, {"type": "prepayment", "sum": 400}],
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
        assert created_payments[0]["externalId"] == "komtet-5000-card"
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
        if request.url.path == "/api/v5/orders" and request.method == "GET":
            return httpx.Response(200, json={"orders": []})
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
        done_body = json.dumps({
            "id": 6600, "external_id": "1500", "state": "done",
            "items": [{"external_id": "1", "quantity": 1, "total": 100}],
            "payments": [{"type": "card", "sum": 100}],
        })
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


@pytest.mark.asyncio
async def test_utc_datetime_serialization_courier_validation_and_clear_queue_id(
    tmp_path: Path,
) -> None:
    """Verify naive SQLite datetimes serialize as UTC ISO, courier komtet_code validation, and komtet_queue_id=None."""
    from pydantic import ValidationError
    from app.schemas import (
        MappingItemSchema,
        OrderSyncItemSchema,
        SettingItemSchema,
        SyncLogItemSchema,
    )

    # 1) Direct schema UTC serialization for naive datetimes (as returned by SQLite)
    naive_dt = datetime(2026, 11, 19, 10, 30, 45)
    setting_schema = SettingItemSchema(
        key="default_vat", value="22", value_type="string", updated_at=naive_dt
    )
    log_schema = SyncLogItemSchema(
        id=1,
        direction="crm_to_komtet",
        level="INFO",
        event_type="order_create",
        message="ok",
        created_at=naive_dt,
    )
    order_schema = OrderSyncItemSchema(
        id=1,
        retailcrm_order_id=10,
        sync_status="synced",
        last_synced_at=naive_dt,
        last_komtet_callback_at=naive_dt,
        updated_at=naive_dt,
    )

    for iso_str in (
        setting_schema.model_dump(mode="json")["updated_at"],
        log_schema.model_dump(mode="json")["created_at"],
        order_schema.model_dump(mode="json")["last_synced_at"],
        order_schema.model_dump(mode="json")["last_komtet_callback_at"],
        order_schema.model_dump(mode="json")["updated_at"],
    ):
        assert isinstance(iso_str, str)
        assert iso_str.endswith("Z") or iso_str.endswith("+00:00")
        assert datetime.fromisoformat(iso_str).utcoffset() == timedelta(0)

    # 2) MappingItemSchema validation for courier komtet_code
    valid_courier = MappingItemSchema(
        mapping_type="courier", retailcrm_code="5", komtet_code=" 701 "
    )
    assert valid_courier.komtet_code == "701"

    empty_courier = MappingItemSchema(
        mapping_type="courier", retailcrm_code="5", komtet_code=""
    )
    assert empty_courier.komtet_code == ""

    non_courier = MappingItemSchema(
        mapping_type="payment_type", retailcrm_code="cash", komtet_code="cash"
    )
    assert non_courier.komtet_code == "cash"

    for bad_code in ("abc", "0", "-5", "12.5", "701a"):
        with pytest.raises(ValidationError):
            MappingItemSchema(
                mapping_type="courier", retailcrm_code="5", komtet_code=bad_code
            )

    # 3) HTTP-level checks: clearing komtet_queue_id=None, 422 on invalid courier, UTC timestamps in JSON
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'preprod.db').as_posix()}"
    engine = create_engine(db_url)
    test_settings = _test_env_settings(komtet_queue_id=12345)
    await init_db(engine, seed=True, env_settings=test_settings)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as seed_db:
        seed_db.add(
            OrderSync(
                retailcrm_order_id=4242,
                sync_status="synced",
                last_synced_at=naive_dt,
                last_komtet_callback_at=naive_dt,
                updated_at=naive_dt,
            )
        )
        seed_db.add(
            SyncLog(
                direction="crm_to_komtet",
                level="INFO",
                event_type="test_utc",
                retailcrm_order_id=4242,
                message="UTC check",
                created_at=naive_dt,
            )
        )
        await seed_db.commit()

    app = create_app(test_settings)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Initially seeded from env_settings
        r_init = await client.get("/api/v1/settings")
        assert r_init.status_code == 200
        assert r_init.json()["komtet_queue_id"] == 12345
        for item in r_init.json()["items"]:
            if item["updated_at"] is not None:
                assert item["updated_at"].endswith("Z") or item["updated_at"].endswith("+00:00")

        # Explicitly clear komtet_queue_id to null
        r_clear = await client.put("/api/v1/settings", json={"komtet_queue_id": None})
        assert r_clear.status_code == 200
        assert r_clear.json()["komtet_queue_id"] is None

        # Verify DB value is NULL and runtime config respects NULL despite env_settings=12345
        async with session_maker() as check_db:
            q_row = await check_db.scalar(
                select(Setting).where(Setting.key == "komtet_queue_id")
            )
            assert q_row is not None
            assert q_row.value is None
            cfg = await load_runtime_config(check_db, env_settings=test_settings)
            assert cfg.komtet_queue_id is None

        # Partial update without komtet_queue_id keeps it None; setting int restores it
        r_partial = await client.put("/api/v1/settings", json={"default_vat": "10"})
        assert r_partial.status_code == 200
        assert r_partial.json()["komtet_queue_id"] is None

        r_set_q = await client.put("/api/v1/settings", json={"komtet_queue_id": 54321})
        assert r_set_q.status_code == 200
        assert r_set_q.json()["komtet_queue_id"] == 54321

        # Invalid courier komtet_code rejected with 422
        r_bad_map = await client.put(
            "/api/v1/mappings",
            json={
                "items": [
                    {
                        "mapping_type": "courier",
                        "retailcrm_code": "10",
                        "komtet_code": "not-a-number",
                    }
                ],
                "replace_types": [],
            },
        )
        assert r_bad_map.status_code == 422

        # Logs and orders-sync endpoints return UTC ISO timestamps
        r_logs = await client.get("/api/v1/logs?order_id=4242")
        assert r_logs.status_code == 200
        created_at_iso = r_logs.json()["items"][0]["created_at"]
        assert created_at_iso.endswith("Z") or created_at_iso.endswith("+00:00")

        r_orders = await client.get("/api/v1/orders-sync")
        assert r_orders.status_code == 200
        ord_item = r_orders.json()["items"][0]
        for field in ("last_synced_at", "last_komtet_callback_at", "updated_at"):
            assert ord_item[field].endswith("Z") or ord_item[field].endswith("+00:00")

    await engine.dispose()


def test_normalize_phone_delimiters_and_customer_fallback() -> None:
    """[HIGH #1] Multiple phones separated by ? / | and fallback when order.phone is invalid."""
    from app.services.mapper import extract_client_phone

    assert normalize_phone("89991112233? 89994445566") == "+79991112233"
    assert normalize_phone("+79991112233? +79994445566") == "+79991112233"
    assert normalize_phone("8 (999) 111-22-33 / 8 (916) 444-55-66") == "+79991112233"
    assert normalize_phone("89991112233 | 89164445566") == "+79991112233"
    assert normalize_phone("12345 / +7 916 555-00-11") == "+79165550011"
    assert normalize_phone("4951234") == "+4951234"

    # Concatenated >15 digits without supported delimiter must be rejected so fallback triggers
    assert normalize_phone("89991112233 89994445566") == ""

    order_with_bad_primary = {
        "phone": "89991112233 89994445566",
        "customer": {
            "phones": [
                {"number": "89991112233 89994445566"},
                {"number": "+7 (916) 000-11-22"},
            ]
        },
    }
    assert extract_client_phone(order_with_bad_primary) == "+79160001122"


@pytest.mark.asyncio
async def test_allowed_delivery_codes_filtering(tmp_path: Path) -> None:
    """[HIGH #2] Filter outbound sync by allowed_delivery_codes while allowing manual override."""
    from app.services.settings_service import update_settings
    from app.schemas import SettingsUpdateRequest

    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'deliv_filter.db').as_posix()}"
    engine = create_engine(db_url)
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    orders_by_id: dict[int, dict[str, Any]] = {
        315270: {
            "id": 315270,
            "number": "304237",
            "site": "main-site",
            "status": "new",
            "phone": "+79161112233",
            "summ": 3000.0,
            "delivery": {"code": "self-delivery", "cost": 0, "address": {"text": "Самовывоз"}},
            "items": [
                {
                    "id": 1,
                    "initialPrice": 3000.0,
                    "quantity": 1,
                    "offer": {"id": 10, "name": "Корм", "vatRate": "10.00"},
                }
            ],
            "customFields": {},
        },
        315271: {
            "id": 315271,
            "number": "304238",
            "site": "main-site",
            "status": "new",
            "phone": "+79161112244",
            "summ": 1500.0,
            "delivery": {"code": "courier", "cost": 300.0, "address": {"text": "Москва, Арбат 1"}},
            "items": [
                {
                    "id": 2,
                    "initialPrice": 1500.0,
                    "quantity": 1,
                    "offer": {"id": 11, "name": "Поводок", "vatRate": "10.00"},
                }
            ],
            "customFields": {},
        },
    }
    komtet_posts: list[dict[str, Any]] = []

    def crm_handler(request: httpx.Request) -> httpx.Response:
        for oid, ord_data in orders_by_id.items():
            if request.url.path == f"/api/v5/orders/{oid}" and request.method == "GET":
                return httpx.Response(200, json={"success": True, "order": ord_data})
            if request.url.path == f"/api/v5/orders/{oid}/edit" and request.method == "POST":
                return httpx.Response(200, json={"success": True, "id": oid})
        return httpx.Response(404, json={"success": False})

    def komtet_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/shop/v2/orders":
            body = json.loads(request.content.decode("utf-8"))
            komtet_posts.append(body)
            return httpx.Response(
                200,
                json={"id": 9100 + len(komtet_posts), "external_id": body["external_id"], "state": "new"},
            )
        return httpx.Response(200, json={})

    async with (
        session_maker() as db,
        httpx.AsyncClient(transport=httpx.MockTransport(crm_handler)) as crm_http,
        httpx.AsyncClient(transport=httpx.MockTransport(komtet_handler)) as k_http,
    ):
        crm_cli = RetailCRMClient("https://example.retailcrm.ru", "k", client=crm_http)
        komtet_cli = KomtetClient("test-shop-id", "test-komtet-secret-8888", client=k_http)

        # Configure allowlist = ["courier"]
        await update_settings(
            db,
            SettingsUpdateRequest(allowed_delivery_codes=["courier"]),
            env_settings=_test_env_settings(),
        )

        # 1) self-delivery order must be skipped with no KOMTET call and no phantom OrderSync row
        res_skip = await sync_retailcrm_to_komtet(
            db, 315270, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res_skip["status"] == "skipped"
        assert res_skip["reason"] == "delivery_type_not_allowed"
        assert res_skip["delivery_code"] == "self-delivery"
        assert len(komtet_posts) == 0

        phantom_sync = await db.scalar(
            select(OrderSync).where(OrderSync.retailcrm_order_id == 315270)
        )
        assert phantom_sync is None

        skip_log = await db.scalar(
            select(SyncLog)
            .where(
                SyncLog.retailcrm_order_id == 315270,
                SyncLog.event_type == "delivery_type_not_allowed",
            )
            .order_by(SyncLog.id.desc())
        )
        assert skip_log is not None

        # 2) courier order is created normally
        res_ok = await sync_retailcrm_to_komtet(
            db, 315271, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res_ok["status"] == "created"
        assert len(komtet_posts) == 1
        assert komtet_posts[0]["external_id"] == "304238"

        # 3) Manual override (`ignore_status_filter=True`) bypasses delivery filter
        res_manual = await sync_retailcrm_to_komtet(
            db,
            315270,
            trigger_event="manual",
            force=True,
            ignore_status_filter=True,
            crm_client=crm_cli,
            komtet_client=komtet_cli,
        )
        assert res_manual["status"] == "created"
        assert len(komtet_posts) == 2

        # 4) Empty allowlist [] allows all delivery types (backward compatibility)
        await update_settings(
            db,
            SettingsUpdateRequest(allowed_delivery_codes=[]),
            env_settings=_test_env_settings(),
        )
        orders_by_id[315272] = {
            **orders_by_id[315270],
            "id": 315272,
            "number": "304239",
        }
        res_any = await sync_retailcrm_to_komtet(
            db, 315272, crm_client=crm_cli, komtet_client=komtet_cli
        )
        assert res_any["status"] == "created"
        assert len(komtet_posts) == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_komtet_webhook_verifies_hmac_before_json_parsing_and_health_log_filter(
    tmp_path: Path,
) -> None:
    """[LOW #8] Verify HMAC runs before JSON parsing and health filter drops GET /health 200."""
    import logging
    from app.main import _HealthCheckAccessFilter

    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'hmac_first.db').as_posix()}"
    engine = create_engine(db_url)
    prod_settings = _test_env_settings(
        app_env="production",
        admin_api_token="prod-admin-token",
        retailcrm_webhook_token="prod-wh-token",
    )
    await init_db(engine, seed=True, env_settings=prod_settings)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    app = create_app(prod_settings)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1) Invalid JSON + missing/bad signature -> 401 (never 400)
        r_bad_txt = await client.post(
            "/webhooks/komtet",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert r_bad_txt.status_code == 401

        # 2) Non-UTF8 garbage + bad signature -> 401 (never 500 UnicodeDecodeError)
        r_bad_bytes = await client.post(
            "/webhooks/komtet",
            content=b"\xff\xfe\xfd",
            headers={"Content-Type": "application/json", "X-HMAC-Signature": "deadbeef"},
        )
        assert r_bad_bytes.status_code == 401

        # 3) Valid HMAC over non-JSON body -> passes HMAC, rejected with 400 Invalid JSON payload
        good_sig_bad_json = KomtetClient.compute_signature(
            "test-komtet-secret-8888",
            "POST",
            "https://sync.example.com/webhooks/komtet",
            "not-json",
        )
        r_valid_sig_bad_json = await client.post(
            "/webhooks/komtet",
            content=b"not-json",
            headers={
                "Content-Type": "application/json",
                "X-HMAC-Signature": good_sig_bad_json,
            },
        )
        assert r_valid_sig_bad_json.status_code == 400

    await engine.dispose()

    # Check _HealthCheckAccessFilter behavior
    flt = _HealthCheckAccessFilter()
    rec_health_200 = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/health", "1.1", 200),
        None,
    )
    rec_api_200 = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/api/v1/health", "1.1", 200),
        None,
    )
    rec_health_500 = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/health", "1.1", 500),
        None,
    )
    assert flt.filter(rec_health_200) is False
    assert flt.filter(rec_api_200) is True
    assert flt.filter(rec_health_500) is True


@pytest.mark.asyncio
async def test_admin_audit_logs_settings_and_mappings_redact_secrets(tmp_path: Path) -> None:
    """Verify PUT /settings and PUT /mappings write system audit logs without leaking secrets."""
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'audit.db').as_posix()}"
    engine = create_engine(db_url)
    test_settings = _test_env_settings()
    await init_db(engine, seed=True, env_settings=test_settings)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    app = create_app(test_settings)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1) Update non-secret and secret settings -> secrets must be stored as "[UPDATED]" only
        r_set = await client.put(
            "/api/v1/settings",
            headers={"X-Forwarded-For": "203.0.113.10, 10.0.0.1"},
            json={
                "auto_sync_enabled": False,
                "default_vat": "10",
                "fallback_client_email": " example@example.com ",
                "trigger_statuses": ["new", "paid"],
                "allowed_delivery_codes": ["courier"],
                "retailcrm_api_key": "super-secret-crm-value-7777",
                "komtet_secret_key": "super-secret-komtet-value-6666",
            },
        )
        assert r_set.status_code == 200
        assert r_set.json()["fallback_client_email"] == "example@example.com"

        async with session_maker() as db:
            row = await db.scalar(
                select(SyncLog)
                .where(SyncLog.event_type == "settings_updated")
                .order_by(SyncLog.id.desc())
            )
            assert row is not None
            assert row.direction == "system"
            assert row.level == "INFO"
            assert row.message == "Обновлены настройки через панель управления"
            assert row.payload is not None
            assert row.payload["client_ip"] == "203.0.113.10"
            assert set(row.payload["changed_fields"]) == {
                "auto_sync_enabled",
                "default_vat",
                "fallback_client_email",
                "trigger_statuses",
                "allowed_delivery_codes",
                "retailcrm_api_key",
                "komtet_secret_key",
            }
            assert row.payload["values"] == {
                "auto_sync_enabled": False,
                "default_vat": "10",
                "fallback_client_email": "example@example.com",
                "trigger_statuses": ["new", "paid"],
                "allowed_delivery_codes": ["courier"],
                "retailcrm_api_key": "[UPDATED]",
                "komtet_secret_key": "[UPDATED]",
            }
            dumped = json.dumps(row.payload, ensure_ascii=False)
            assert "super-secret-crm-value" not in dumped
            assert "super-secret-komtet-value" not in dumped
            assert "7777" not in dumped
            assert "6666" not in dumped
            assert "7777" not in row.message
            assert "6666" not in row.message

        # 2) Sending masked secret placeholder must NOT log secret key as changed
        r_set_masked = await client.put(
            "/api/v1/settings",
            headers={"X-Real-IP": "203.0.113.11"},
            json={
                "retailcrm_api_key": "****7777",
                "final_status": "delivered",
            },
        )
        assert r_set_masked.status_code == 200

        async with session_maker() as db:
            row2 = await db.scalar(
                select(SyncLog)
                .where(SyncLog.event_type == "settings_updated")
                .order_by(SyncLog.id.desc())
            )
            assert row2 is not None
            assert row2.payload is not None
            assert row2.payload["client_ip"] == "203.0.113.11"
            assert row2.payload["changed_fields"] == ["final_status"]
            assert row2.payload["values"] == {"final_status": "delivered"}
            assert "retailcrm_api_key" not in row2.payload["values"]

        # 3) Update mappings -> system audit log recorded with replace_types, count, client_ip
        r_map = await client.put(
            "/api/v1/mappings",
            headers={"X-Real-IP": "198.51.100.5"},
            json={
                "items": [
                    {
                        "mapping_type": "courier",
                        "retailcrm_code": "1",
                        "retailcrm_name": "Иван",
                        "komtet_code": "701",
                        "komtet_name": "Курьер Иван",
                        "is_active": True,
                    },
                    {
                        "mapping_type": "courier",
                        "retailcrm_code": "2",
                        "retailcrm_name": "Петр",
                        "komtet_code": "702",
                        "komtet_name": "Курьер Петр",
                        "is_active": True,
                    },
                ],
                "replace_types": ["courier"],
            },
        )
        assert r_map.status_code == 200

        async with session_maker() as db:
            map_log = await db.scalar(
                select(SyncLog)
                .where(SyncLog.event_type == "mappings_updated")
                .order_by(SyncLog.id.desc())
            )
            assert map_log is not None
            assert map_log.direction == "system"
            assert map_log.level == "INFO"
            assert map_log.message == "Обновлены соответствия через панель управления"
            assert map_log.payload == {
                "replace_types": ["courier"],
                "count": 2,
                "client_ip": "198.51.100.5",
            }

        # 4) GET /api/v1/logs?direction=system returns all 3 audit entries
        r_sys_logs = await client.get("/api/v1/logs?direction=system")
        assert r_sys_logs.status_code == 200
        sys_data = r_sys_logs.json()
        assert sys_data["total"] == 3
        assert len(sys_data["items"]) == 3
        assert all(item["direction"] == "system" for item in sys_data["items"])

    await engine.dispose()


@pytest.mark.asyncio
async def test_http_clients_structured_logging_and_no_secret_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify RetailCRMClient & KomtetClient emit timed logs with retries/errors and never leak secrets."""
    import logging

    crm_attempts = {"order": 0}
    komtet_attempts = {"create": 0}

    def crm_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/credentials":
            return httpx.Response(200, json={"success": True, "sitesAvailable": ["main"]})
        if request.url.path == "/api/v5/orders/101" and request.method == "GET":
            crm_attempts["order"] += 1
            if crm_attempts["order"] == 1:
                return httpx.Response(429, json={"success": False, "errorMsg": "Rate limit"})
            return httpx.Response(200, json={"success": True, "order": {"id": 101}})
        if request.url.path == "/api/v5/orders/101/edit" and request.method == "POST":
            return httpx.Response(
                422,
                json={
                    "success": False,
                    "errorMsg": "Validation failed",
                    "errors": {"phone": "bad"},
                },
            )
        return httpx.Response(404, json={"success": False})

    def komtet_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/shop/v2/queues/1":
            return httpx.Response(200, json={"id": 1, "state": "active"})
        if request.url.path == "/api/shop/v2/orders" and request.method == "POST":
            komtet_attempts["create"] += 1
            if komtet_attempts["create"] == 1:
                return httpx.Response(429, text="Rate limited")
            return httpx.Response(200, json={"id": 500, "state": "new"})
        if request.url.path == "/api/shop/v2/orders/99" and request.method == "PUT":
            return httpx.Response(
                422,
                json={"title": "Bad order", "code": "ERR422", "description": "Invalid total"},
            )
        return httpx.Response(404)

    with caplog.at_level(logging.INFO, logger="app"):
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(crm_handler)) as crm_http,
            httpx.AsyncClient(transport=httpx.MockTransport(komtet_handler)) as komtet_http,
        ):
            crm = RetailCRMClient(
                "https://example.retailcrm.ru",
                "crm-ultra-secret-token",
                retry_delays=(0.0,),
                client=crm_http,
            )
            komtet = KomtetClient(
                "shop-1",
                "komtet-ultra-secret-key",
                retry_delays=(0.0,),
                client=komtet_http,
            )

            await crm.check_credentials()
            await crm.get_order(101, site="main")
            with pytest.raises(RetailCRMError):
                await crm.edit_order(101, {"status": "complete"})

            await komtet.get_queue(1)
            await komtet.create_order({"external_id": "101"})
            with pytest.raises(KomtetError):
                await komtet.update_order(99, {"external_id": "101"})

    log_text = caplog.text
    assert "crm-ultra-secret-token" not in log_text
    assert "komtet-ultra-secret-key" not in log_text
    assert "[RetailCRM] GET /api/credentials -> 200 (" in log_text
    assert "[RetailCRM] retry 1/1 after GET /api/v5/orders/101 -> 429 (delay=0.0s)" in log_text
    assert "[RetailCRM] GET /api/v5/orders/101 -> 200 (" in log_text
    assert "[RetailCRM] POST /api/v5/orders/101/edit -> 422 (" in log_text
    assert "[KOMTET] GET /api/shop/v2/queues/1 -> 200 (" in log_text
    assert "[KOMTET] retry 1/1 after POST /api/shop/v2/orders -> 429 (delay=0.0s)" in log_text
    assert "[KOMTET] POST /api/shop/v2/orders -> 200 (" in log_text
    assert "[KOMTET] PUT /api/shop/v2/orders/99 -> 422 (" in log_text
    assert "ms)" in log_text


def test_delivery_payload_version_and_largest_remainder() -> None:
    order = {"id": 1, "delivery": {"date": "2026-11-21"}}
    for version, date_part in (("v1", "2026-11-21"), ("v2", "21.11.2026")):
        payload, _ = map_retailcrm_order_to_komtet(
            order, _sample_config(komtet_api_version=version), _sample_mappings()
        )
        assert payload["external_id" if version == "v2" else "order_id"] == "1"
        assert payload["date_start"] == f"{date_part} 09:00"
        assert payload["date_end"] == f"{date_part} 21:00"

    lines = _distribute_discount([Decimal("1.00")] * 4, Decimal("0.02"))
    assert lines == [Decimal("0.99")] * 2 + [Decimal("1.00")] * 2
    assert sum(lines) == Decimal("3.98")


@pytest.mark.asyncio
async def test_create_order_does_not_retry_after_read_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("Response lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KomtetClient("shop", "secret", client=http, retry_delays=(0.0,))
        with pytest.raises(KomtetError):
            await client.create_order({"external_id": "42"})
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["mixed", "switch", "refund", "mismatch", "missing", "stale"])
async def test_done_callback_reconciliation(tmp_path: Path, scenario: str) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'done.db').as_posix()}")
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    calls = []
    new_payments = []
    order = {
        "id": 42, "site": "main-site", "status": "delivering",
        "totalSumm": 1200, "discountManualAmount": 100,
        "items": [{
            "id": 10, "quantity": 2, "initialPrice": 750, "discountTotal": 150,
            "priceType": {"code": "retail"}, "offer": {"id": 1},
        }],
        "payments": {"20": {"id": 20, "type": "cash", "status": "not-paid"}},
    }
    if scenario == "refund":
        order["payments"] = {"21": {"id": 21, "type": "online", "status": "paid", "amount": 700}}
    payments = {
        "mixed": [{"type": "cash", "sum": 300}, {"type": "card", "sum": 300}],
        "switch": [{"type": "card", "sum": 600}],
        "refund": [{"type": "prepayment", "sum": 600}],
        "missing": [],
    }.get(scenario, [{"type": "card", "sum": 600}])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "GET" and path == "/api/shop/v2/orders/99":
            return httpx.Response(200, json={
                "id": 99, "state": "done", "items": [{"external_id": "10", "quantity": 1, "total": 600}],
                "receipt_info": {"payments": payments},
            })
        if path == "/api/v5/orders/42":
            return httpx.Response(200, json={"success": True, "order": order})
        form = parse_qs(request.content.decode())
        if path == "/api/v5/orders/42/edit":
            patch = json.loads(form["order"][0])
            if "items" in patch:
                assert patch["items"][0]["priceType"] == {"code": "retail"}
                assert patch["discountManualAmount"] == 50
                if scenario != "mismatch":
                    order.update(items=patch["items"], totalSumm=600)
            else:
                order.update(patch)
        elif path == "/api/v5/orders/payments/20/delete":
            assert form == {"by": ["id"]}
            del order["payments"]["20"]
        elif path == "/api/v5/orders/payments/20/edit":
            patch = json.loads(form["payment"][0])
            assert "type" not in patch and "id" not in patch
            order["payments"]["20"].update(patch)
        elif path == "/api/v5/orders/payments/create":
            payment = json.loads(form["payment"][0])
            new_payments.append(payment)
            order["payments"]["22"] = {**payment, "id": 22}
        else:
            pytest.fail(f"Unexpected endpoint {path}")
        return httpx.Response(200, json={"success": True})

    async with sessions() as session, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        row = OrderSync(retailcrm_order_id=42, komtet_order_id=99, komtet_external_id="42")
        session.add(row)
        await session.commit()
        crm = RetailCRMClient("https://example.retailcrm.ru", "key", client=http)
        komtet = KomtetClient("shop", "secret", client=http)
        payload = {"id": 98 if scenario == "stale" else 99, "external_id": "42", "state": "done"}

        async def sync():
            return await sync_komtet_to_retailcrm(
                session, payload, crm_client=crm, komtet_client=komtet, signature_already_verified=True
            )

        result = await sync()
        if scenario == "stale":
            assert result == {"status": "skipped", "reason": "stale_callback_conflict"}
            assert not calls and row.komtet_order_id == 99
        else:
            assert calls[0] == ("GET", "/api/shop/v2/orders/99")
        if scenario in ("mixed", "switch"):
            assert result["status"] == "processed"
            assert calls[1:4] == [
                ("GET", "/api/v5/orders/42"), ("POST", "/api/v5/orders/42/edit"),
                ("GET", "/api/v5/orders/42"),
            ]
            assert new_payments[0]["externalId"] == "komtet-99-card"
            assert new_payments[0]["type"] == "bank-card"
            assert new_payments[0]["status"] == "paid"
            if scenario == "mixed":
                assert order["payments"]["20"]["externalId"] == "komtet-99-cash"
                assert order["payments"]["20"]["amount"] == new_payments[0]["amount"] == 300
            else:
                assert calls[4:6] == [
                    ("POST", "/api/v5/orders/payments/20/delete"),
                    ("POST", "/api/v5/orders/payments/create"),
                ]
                assert new_payments[0]["amount"] == 600
            # Simulate a retry after remote payment succeeded but the local hash was lost.
            row.last_komtet_callback_hash = None
            await session.commit()
            assert (await sync())["payment_action"] == "none"
            assert len(new_payments) == 1
        elif scenario == "refund":
            assert result["status"] == row.sync_status == "refund_required"
            assert "100.00" in row.last_error
        elif scenario in ("missing", "mismatch"):
            assert result["status"] == row.sync_status == "pending_reconciliation"
        if scenario not in ("mixed", "switch"):
            assert not new_payments
            assert order["status"] == "delivering"

    await engine.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize("purchased_qty", [2, 3])
async def test_done_callback_reaggregates_marked_units(tmp_path: Path, purchased_qty: int) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'marked.db').as_posix()}")
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    final_total = 450 * purchased_qty
    order = {
        "id": 315568, "site": "main-site", "status": "complete", "totalSumm": 1350,
        "items": [{"id": 473148, "quantity": 3, "initialPrice": 450, "offer": {"id": 1}}],
        "payments": {"20": {
            "id": 20, "externalId": "komtet-99-card", "type": "bank-card",
            "status": "paid", "amount": final_total,
        }},
    }
    edits = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/shop/v2/orders/99":
            return httpx.Response(200, json={
                "id": 99, "state": "done",
                "items": [
                    {"external_id": f"473148_m{index}", "quantity": 1, "price": 450, "total": 450}
                    for index in range(1, purchased_qty + 1)
                ],
                "receipt_info": {"payments": [{"type": "card", "sum": final_total}]},
            })
        if request.method == "GET" and path == "/api/v5/orders/315568":
            return httpx.Response(200, json={"success": True, "order": order})
        if request.method == "POST" and path == "/api/v5/orders/315568/edit":
            patch = json.loads(parse_qs(request.content.decode())["order"][0])
            edits.append(patch)
            order["items"] = patch["items"]
            order["totalSumm"] = sum(item["initialPrice"] * item["quantity"] for item in patch["items"])
            return httpx.Response(200, json={"success": True})
        pytest.fail(f"Unexpected endpoint {request.method} {path}")

    async with sessions() as session, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        session.add(OrderSync(retailcrm_order_id=315568, komtet_order_id=99, komtet_external_id="304543"))
        await session.commit()
        result = await sync_komtet_to_retailcrm(
            session, {"id": 99, "external_id": "304543", "state": "done"},
            crm_client=RetailCRMClient("https://example.retailcrm.ru", "key", client=http),
            komtet_client=KomtetClient("shop", "secret", client=http),
            signature_already_verified=True,
        )
        assert result["status"] == "processed"
        assert result["items_changed"] is (purchased_qty == 2)
        assert len(edits) == 3 - purchased_qty
        assert [(item["id"], item["quantity"]) for item in order["items"]] == [(473148, purchased_qty)]
        assert order["totalSumm"] == final_total

    await engine.dispose()


@pytest.mark.asyncio
async def test_reconciliation_retries_recent_failed_order(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'retry.db').as_posix()}")
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    order = {
        "id": 42, "status": "delivering", "site": "main-site",
        "items": [{"id": 10, "initialPrice": 100, "quantity": 1, "productName": "Корм"}],
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append((request.method, path))
        if path == "/api/v5/orders/42":
            return httpx.Response(200, json={"success": True, "order": order})
        if path == "/api/shop/v2/orders":
            return httpx.Response(201, json={"id": 99, "state": "new"})
        if path == "/api/v5/orders/42/edit":
            return httpx.Response(200, json={"success": True})
        pytest.fail(f"Unexpected endpoint {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        crm = RetailCRMClient("https://example.retailcrm.ru", "key", client=http)
        komtet = KomtetClient("shop", "secret", client=http)
        async with sessions.begin() as db:
            db.add_all([
                OrderSync(retailcrm_order_id=42, sync_status="error", last_error="temporary"),
                OrderSync(retailcrm_order_id=43, sync_status="error", komtet_state="done"),
                OrderSync(retailcrm_order_id=44, sync_status="error",
                          updated_at=datetime.now(timezone.utc) - timedelta(hours=49)),
            ])
            setting = await db.scalar(select(Setting).where(Setting.key == "auto_sync_enabled"))
            setting.value = "false"
        await run_reconciliation_cycle(sessions, crm_client=crm, komtet_client=komtet)
        assert not calls
        async with sessions.begin() as db:
            setting = await db.scalar(select(Setting).where(Setting.key == "auto_sync_enabled"))
            setting.value = "true"
        await run_reconciliation_cycle(sessions, crm_client=crm, komtet_client=komtet)
        assert calls == [
            ("GET", "/api/v5/orders/42"), ("POST", "/api/shop/v2/orders"),
            ("POST", "/api/v5/orders/42/edit"),
        ]
        async with sessions() as db:
            row = await db.scalar(select(OrderSync).where(OrderSync.retailcrm_order_id == 42))
            assert (row.sync_status, row.komtet_order_id, row.last_error) == ("synced", 99, None)
            log = await db.scalar(select(SyncLog).where(SyncLog.event_type == "order_create"))
            assert log.payload["trigger_event"] == "auto_retry"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_final_status_once", [False, True])
async def test_reconciliation_polls_missed_callback_without_duplicate_payments(
    tmp_path: Path, fail_final_status_once: bool,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'poll.db').as_posix()}")
    await init_db(engine, seed=True, env_settings=_test_env_settings())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    order = {
        "id": 42, "status": "delivering", "site": "main-site", "totalSumm": 200,
        "items": [{"id": 10, "initialPrice": 100, "quantity": 2}], "payments": {},
    }
    detail = {
        "id": 99, "state": "done", "items": [{"external_id": "10", "quantity": 1, "total": 100}],
        "receipt_info": {"payments": [{"type": "card", "sum": 100}]},
    }
    writes = []
    polls = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_final_status_once
        path = request.url.path
        if request.method == "GET" and path.startswith("/api/shop/v2/orders/"):
            polls.append(path)
            if path.endswith("/97"):
                return httpx.Response(404)
            if path.endswith("/98"):
                return httpx.Response(200, json={"id": 98, "state": "canceled"})
            assert path.endswith("/99")
            return httpx.Response(200, json=detail)
        if request.method == "GET" and path == "/api/v5/orders/42":
            return httpx.Response(200, json={"success": True, "order": order})
        writes.append(path)
        form = parse_qs(request.content.decode())
        if path == "/api/v5/orders/payments/create":
            order["payments"]["20"] = {**json.loads(form["payment"][0]), "id": 20}
        elif path == "/api/v5/orders/42/edit":
            patch = json.loads(form["order"][0])
            if patch.get("status") == "complete" and fail_final_status_once:
                fail_final_status_once = False
                return httpx.Response(400, json={"success": False, "errorMsg": "Edit conflict"})
            order.update(patch)
            if "items" in patch:
                order["totalSumm"] = 100
        else:
            pytest.fail(f"Unexpected endpoint {path}")
        return httpx.Response(200, json={"success": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        crm = RetailCRMClient("https://example.retailcrm.ru", "key", client=http)
        komtet = KomtetClient("shop", "secret", client=http)
        async with sessions.begin() as db:
            db.add_all([
                OrderSync(retailcrm_order_id=40, komtet_order_id=97, sync_status="synced"),
                OrderSync(retailcrm_order_id=41, komtet_order_id=98, sync_status="synced"),
                OrderSync(retailcrm_order_id=42, komtet_order_id=99, sync_status="synced"),
                OrderSync(retailcrm_order_id=43, komtet_order_id=100, sync_status="synced",
                          updated_at=datetime.now(timezone.utc) - timedelta(hours=73)),
            ])
        interrupted = fail_final_status_once
        await run_reconciliation_cycle(sessions, crm_client=crm, komtet_client=komtet)
        assert polls == [f"/api/shop/v2/orders/{order_id}" for order_id in (97, 98, 99)]
        await run_reconciliation_cycle(sessions, crm_client=crm, komtet_client=komtet)
        expected_polls = [f"/api/shop/v2/orders/{order_id}" for order_id in (97, 98, 99, 97)]
        if interrupted:
            expected_polls.append("/api/shop/v2/orders/99")
        assert polls == expected_polls
        async with sessions() as db:
            canceled = await db.scalar(select(OrderSync).where(OrderSync.komtet_order_id == 98))
            completed = await db.scalar(select(OrderSync).where(OrderSync.komtet_order_id == 99))
            assert (canceled.komtet_state, canceled.sync_status) == ("canceled", "canceled")
            assert (completed.komtet_state, completed.sync_status) == ("done", "completed")
            # A delayed callback can arrive with a different payload from the GET response.
            await sync_komtet_to_retailcrm(
                db, {**detail, "external_id": "42"}, crm_client=crm, komtet_client=komtet,
                signature_already_verified=True,
            )
        expected_writes = ["/api/v5/orders/42/edit", "/api/v5/orders/payments/create", "/api/v5/orders/42/edit"]
        if interrupted:
            expected_writes.append("/api/v5/orders/42/edit")
        assert writes == expected_writes
        assert order["items"][0]["quantity"] == 1
        assert order["status"] == "complete"
        assert order["payments"]["20"]["status"] == "paid"
        assert order["payments"]["20"]["amount"] == 100
    await engine.dispose()


@pytest.mark.parametrize(("content_type", "body"), [
    ("", b"order_id=315454."),
    ("application/x-www-form-urlencoded", b"order_id=315454%0A"),
    ("application/x-www-form-urlencoded", b"order_id =315454"),
    ("application/x-www-form-urlencoded", b"order_id+=315454"),
    ("text/plain", b"order_id=315454"),
    ("multipart/form-data; boundary=TestABC",
     b'--TestABC\r\nContent-Disposition: form-data; name="order_id"\r\n\r\n315454\r\n--TestABC--\r\n'),
])
def test_retailcrm_body_order_id(content_type, body):
    fields = webhooks._body_fields(body, content_type)
    assert webhooks._extract_webhook_fields(fields)[0] == 315454


def test_retailcrm_invalid_order_preview():
    assert webhooks._parse_positive_int("{{ order.id }}") is None
    preview = webhooks._body_preview(b"order_id=%7B%7B+order.id+%7D%7D&%74oken=secret")
    assert "{{ order.id }}" in preview
    assert "secret" not in preview
