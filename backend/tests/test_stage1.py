"""Unit and integration-smoke tests for Stage 1 backend skeleton, DB, and API clients."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.clients.komtet import KomtetClient, KomtetError  # noqa: E402
from app.clients.retailcrm import RetailCRMClient, RetailCRMError  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import Base, cleanup_old_logs, create_engine, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Mapping, OrderSync, Setting, SyncLog  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402


def test_komtet_hmac_matches_official_sdk_vectors() -> None:
    """Verify HMAC-MD5 signatures match exact Komtet Python SDK test vectors."""
    # Vector from tests/v1/client/test.py
    sig_v1_get = KomtetClient.compute_signature(
        "secret-key",
        "GET",
        "https://kassa.komtet.ru/api/shop/v1/queues/1",
    )
    assert sig_v1_get == "b72f98703ccaea912cdf06e364d81885"

    # Vector from tests/v2/client/test.py
    sig_v2_get = KomtetClient.compute_signature(
        "secret-key",
        "GET",
        "https://kassa.komtet.ru/api/shop/v2/queues/1",
    )
    assert sig_v2_get == "4f055e21e84b0d014b3e0b235e391c64"

    # Vector from tests/v2/client/test.py POST with Decimal serialization
    sig_v2_post = KomtetClient.compute_signature(
        "secret-key",
        "POST",
        "https://kassa.komtet.ru/api/shop/v2/queues/3/task",
        '{"key": 10.0}',
    )
    assert sig_v2_post == "d5aa2f6177ba2f4f94218ed9587454be"

    assert KomtetClient.verify_signature(
        "secret-key",
        "POST",
        "https://kassa.komtet.ru/api/shop/v2/queues/3/task",
        '{"key": 10.0}',
        sig_v2_post,
    )
    assert not KomtetClient.verify_signature(
        "secret-key",
        "POST",
        "https://kassa.komtet.ru/api/shop/v2/queues/3/task",
        '{"key": 10.0}',
        "deadbeef",
    )


@pytest.mark.asyncio
async def test_komtet_client_headers_query_signature_and_orders() -> None:
    """Verify KomtetClient signs exact query URL and JSON body on GET/POST/PUT."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/shop/v1/queues/23881":
            return httpx.Response(200, json={"id": 23881, "state": "active"})
        if request.url.path == "/api/shop/v1/employees":
            return httpx.Response(
                200,
                json={"account_employees": [{"id": 10, "name": "Courier 1"}], "meta": {"total": 1}},
            )
        if request.url.path == "/api/shop/v2/orders" and request.method == "POST":
            return httpx.Response(200, json={"id": 775, "external_id": "12", "state": "new"})
        if request.url.path == "/api/shop/v2/orders/999":
            return httpx.Response(
                422,
                json={"title": "Validation error", "code": "INVALID", "description": "Bad field"},
            )
        return httpx.Response(404, json={"title": "Not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        komtet = KomtetClient(
            "shop-id",
            "secret-key",
            api_version="v2",
            client=http_client,
        )
        assert await komtet.is_queue_active(23881, api_version="v1") is True
        couriers = await komtet.get_couriers(start=0, limit=10, api_version="v1")
        assert couriers["meta"]["total"] == 1

        created = await komtet.create_order(
            {"external_id": "12", "prepayment": Decimal("10.50")},
            api_version="v2",
        )
        assert created["id"] == 775

        with pytest.raises(KomtetError) as exc_info:
            await komtet.get_order(999, api_version="v2")
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "INVALID"

    # Check GET query signature matches exact serialized URL
    emp_req = captured[1]
    assert emp_req.headers["Authorization"] == "shop-id"
    assert emp_req.headers["Accept"] == "application/json"
    expected_emp_sig = KomtetClient.compute_signature("secret-key", "GET", str(emp_req.url))
    assert emp_req.headers["X-HMAC-Signature"] == expected_emp_sig

    # Check POST JSON body & Content-Type & signature
    post_req = captured[2]
    assert post_req.headers["Content-Type"] == "application/json"
    body_text = post_req.content.decode("utf-8")
    assert json.loads(body_text) == {"external_id": "12", "prepayment": 10.5}
    expected_post_sig = KomtetClient.compute_signature(
        "secret-key", "POST", str(post_req.url), body_text
    )
    assert post_req.headers["X-HMAC-Signature"] == expected_post_sig


@pytest.mark.asyncio
async def test_retailcrm_client_form_encoding_and_custom_field() -> None:
    """Verify RetailCRMClient uses X-API-KEY, bracket params, and form-urlencoded JSON fields."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/v5/custom-fields/order/komtet_id" and request.method == "GET":
            return httpx.Response(404, json={"success": False, "errorMsg": "Not found"})
        if request.url.path == "/api/v5/custom-fields/order/create" and request.method == "POST":
            return httpx.Response(200, json={"success": True, "code": "komtet_id"})
        if request.url.path == "/api/v5/orders/101/edit" and request.method == "POST":
            return httpx.Response(200, json={"success": True, "id": 101})
        if request.url.path == "/api/v5/orders/payments/create" and request.method == "POST":
            return httpx.Response(201, json={"success": True, "id": 555})
        if request.url.path == "/api/v5/custom-fields" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "customFields": []})
        return httpx.Response(200, json={"success": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        crm = RetailCRMClient("https://demo.retailcrm.ru/", "crm-secret", client=http_client)
        await crm.get_custom_fields(entity="order")
        ensure_res = await crm.ensure_komtet_id_field()
        assert ensure_res["created"] is True

        edit_res = await crm.edit_order(101, {"customFields": {"komtet_id": "775"}}, site="main")
        assert edit_res["success"] is True

        pay_res = await crm.create_payment({"order": {"id": 101}, "type": "cash", "amount": 500})
        assert pay_res["id"] == 555

    # Check query param flattening
    cf_list_req = captured[0]
    assert cf_list_req.headers["X-API-KEY"] == "crm-secret"
    assert "filter%5Bentity%5D=order" in str(cf_list_req.url)

    # Check form-urlencoded payload on custom-fields create
    cf_create_req = captured[2]
    form = parse_qs(cf_create_req.content.decode("utf-8"))
    cf_payload = json.loads(form["customField"][0])
    assert cf_payload["code"] == "komtet_id"
    assert cf_payload["type"] == "integer"

    # Check form-urlencoded payload on edit_order
    edit_req = captured[3]
    edit_form = parse_qs(edit_req.content.decode("utf-8"))
    assert edit_form["by"] == ["id"]
    assert edit_form["site"] == ["main"]
    assert json.loads(edit_form["order"][0]) == {"customFields": {"komtet_id": "775"}}


@pytest.mark.asyncio
async def test_retailcrm_error_raises_on_success_false() -> None:
    """Verify RetailCRMClient raises RetailCRMError when success=False even on HTTP 200."""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "errorMsg": "Validation failed", "errors": {"order": "bad"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        crm = RetailCRMClient("https://demo.retailcrm.ru", "crm-secret", client=http_client)
        with pytest.raises(RetailCRMError) as exc_info:
            await crm.get_statuses()
        assert "Validation failed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_database_models_crud(tmp_path: Path) -> None:
    """Verify SQLAlchemy 2.0 async engine, WAL mode, and all 4 ORM models."""
    db_file = tmp_path / "test.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    test_engine = create_engine(db_url)
    await init_db(test_engine, seed=False)

    session_maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_maker() as session:
        session.add(Setting(key="sno", value="1", value_type="int"))
        session.add(
            Mapping(
                mapping_type="payment_type",
                retailcrm_code="cash",
                retailcrm_name="Наличные",
                komtet_code="cash",
                komtet_name="Наличными",
            )
        )
        session.add(
            OrderSync(
                retailcrm_order_id=1001,
                retailcrm_order_number="1001A",
                komtet_order_id=9001,
                sync_status="synced",
                last_hash="abc123",
            )
        )
        session.add(
            SyncLog(
                direction="crm_to_komtet",
                level="INFO",
                event_type="order_create",
                retailcrm_order_id=1001,
                komtet_order_id=9001,
                message="Order synced",
                payload={"items": 2},
            )
        )
        await session.commit()

        found_order = await session.scalar(
            select(OrderSync).where(OrderSync.retailcrm_order_id == 1001)
        )
        assert found_order is not None
        assert found_order.komtet_order_id == 9001

        found_setting = await session.scalar(select(Setting).where(Setting.key == "sno"))
        assert found_setting is not None
        assert found_setting.value == "1"

    await test_engine.dispose()


def test_fastapi_health_endpoints() -> None:
    """Verify FastAPI app boots cleanly and serves health endpoints."""
    test_settings = Settings(
        retailcrm_url="https://demo.retailcrm.ru",
        retailcrm_api_key="demo-key",
        komtet_shop_id="shop",
        komtet_secret_key="secret",
    )
    app = create_app(test_settings)
    with TestClient(app) as client:
        r1 = client.get("/health")
        assert r1.status_code == 200
        assert r1.json()["status"] == "ok"

        r2 = client.get("/api/v1/health")
        assert r2.status_code == 200
        data = r2.json()
        assert data["database"] == "connected"
        assert data["retailcrm_configured"] is True
        assert data["komtet_configured"] is True


@pytest.mark.asyncio
async def test_cleanup_old_logs_deletes_expired_and_keeps_fresh(tmp_path: Path) -> None:
    """Verify cleanup_old_logs removes sync_logs older than retention_days and preserves fresh ones."""
    db_file = tmp_path / "retention.db"
    test_engine = create_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await init_db(test_engine, seed=False)

    now = datetime.now(timezone.utc)
    session_factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        session.add_all(
            [
                SyncLog(
                    direction="crm_to_komtet",
                    level="INFO",
                    event_type="old_45d",
                    message="Expired 45d",
                    created_at=now - timedelta(days=45),
                ),
                SyncLog(
                    direction="komtet_to_crm",
                    level="WARNING",
                    event_type="old_31d",
                    message="Expired 31d",
                    created_at=now - timedelta(days=31),
                ),
                SyncLog(
                    direction="crm_to_komtet",
                    level="INFO",
                    event_type="fresh_10d",
                    message="Fresh 10d",
                    created_at=now - timedelta(days=10),
                ),
                SyncLog(
                    direction="crm_to_komtet",
                    level="INFO",
                    event_type="fresh_now",
                    message="Fresh now",
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    deleted = await cleanup_old_logs(30, target_engine=test_engine)
    assert deleted == 2

    async with session_factory() as session:
        remaining = list((await session.scalars(select(SyncLog).order_by(SyncLog.id))).all())
        assert [row.event_type for row in remaining] == ["fresh_10d", "fresh_now"]

    # Non-positive retention disables deletion safely
    assert await cleanup_old_logs(0, target_engine=test_engine) == 0

    await test_engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_old_logs_batched_and_sqlite_busy_timeout(tmp_path: Path) -> None:
    """Verify batched deletion in cleanup_old_logs and 30s SQLite busy_timeout PRAGMA."""
    from sqlalchemy import text

    db_file = tmp_path / "batched_retention.db"
    test_engine = create_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    await init_db(test_engine, seed=False)

    async with test_engine.connect() as conn:
        busy_timeout = (await conn.execute(text("PRAGMA busy_timeout;"))).scalar_one()
        assert int(busy_timeout) == 30000

    now = datetime.now(timezone.utc)
    session_factory = async_sessionmaker(
        bind=test_engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        rows = [
            SyncLog(
                direction="crm_to_komtet",
                level="INFO",
                event_type=f"expired_{idx}",
                message=f"Expired log {idx}",
                created_at=now - timedelta(days=40 + idx),
            )
            for idx in range(12)
        ] + [
            SyncLog(
                direction="crm_to_komtet",
                level="INFO",
                event_type=f"fresh_{idx}",
                message=f"Fresh log {idx}",
                created_at=now - timedelta(days=idx),
            )
            for idx in range(3)
        ]
        session.add_all(rows)
        await session.commit()

    # Delete across multiple batches (12 expired rows with batch_size=5 -> 3 iterations: 5+5+2)
    deleted = await cleanup_old_logs(30, target_engine=test_engine, batch_size=5)
    assert deleted == 12

    async with session_factory() as session:
        remaining = list((await session.scalars(select(SyncLog).order_by(SyncLog.id))).all())
        assert [row.event_type for row in remaining] == ["fresh_0", "fresh_1", "fresh_2"]

    await test_engine.dispose()


def test_production_hides_openapi_and_disables_default_localhost_cors() -> None:
    """Verify production disables Swagger/OpenAPI and localhost CORS unless explicitly configured."""
    prod_settings = Settings(
        _env_file=None,
        app_env="production",
        admin_api_token="prod-secret-token",
        retailcrm_webhook_token="prod-wh-token",
    )
    assert prod_settings.effective_cors_origins == []

    prod_app = create_app(prod_settings)
    with TestClient(prod_app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404

        cors_resp = client.get("/health", headers={"Origin": "http://localhost:5173"})
        assert cors_resp.status_code == 200
        assert "access-control-allow-origin" not in cors_resp.headers

    # Explicit CORS_ORIGINS in production is honored while localhost remains rejected
    explicit_prod = Settings(
        _env_file=None,
        app_env="production",
        cors_origins=["https://panel.example.com"],
        admin_api_token="prod-secret-token",
        retailcrm_webhook_token="prod-wh-token",
    )
    assert explicit_prod.effective_cors_origins == ["https://panel.example.com"]
    explicit_app = create_app(explicit_prod)
    with TestClient(explicit_app) as client:
        allowed = client.get("/health", headers={"Origin": "https://panel.example.com"})
        assert allowed.headers.get("access-control-allow-origin") == "https://panel.example.com"
        blocked = client.get("/health", headers={"Origin": "http://localhost:5173"})
        assert "access-control-allow-origin" not in blocked.headers


def test_lifespan_skips_log_retention_loop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify lifespan does not schedule _log_retention_loop when LOG_RETENTION_DAYS <= 0."""
    import app.main as main_mod

    started = False

    async def fake_loop(_days: int) -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(main_mod, "_log_retention_loop", fake_loop)

    disabled_settings = Settings(
        _env_file=None,
        app_env="test",
        log_retention_days=0,
    )
    app = create_app(disabled_settings)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert started is False
