"""Webhook endpoints receiving events from RetailCRM and KOMTET Kassa."""

from __future__ import annotations

import json
import logging
import re
import time
from email import policy
from email.parser import BytesParser
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote_plus

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.komtet import KomtetClient
from app.clients.retailcrm import RetailCRMClient
from app.config import Settings
from app.database import get_db
from app.security import get_app_settings, get_client_ip, require_retailcrm_webhook_token
from app.services.settings_service import RuntimeConfig, load_runtime_config
from app.services.sync_service import (
    build_komtet_client,
    build_retailcrm_client,
    sync_komtet_to_retailcrm,
    sync_retailcrm_to_komtet,
    verify_komtet_callback_signature,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_ORDER_ID_KEYS = ("order_id", "orderId", "id", "order[id]")
_SITE_KEYS = ("site", "order[site]")


def get_retailcrm_factory() -> Callable[[RuntimeConfig], RetailCRMClient]:
    """Dependency returning RetailCRMClient factory (overridable in tests)."""
    return build_retailcrm_client


def get_komtet_factory() -> Callable[[RuntimeConfig], KomtetClient]:
    """Dependency returning KomtetClient factory (overridable in tests)."""
    return build_komtet_client


def _parse_positive_int(raw: Any) -> Optional[int]:
    if not isinstance(raw, (str, int)) or isinstance(raw, bool):
        return None
    try:
        digits = str(raw).strip().strip(" \t\r\n.,;:!?'\"()").strip()
        if not digits.removeprefix("+").isdecimal():
            return None
        value = int(digits)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _body_fields(raw_body: bytes, content_type: str) -> dict[str, Any]:
    text = raw_body.decode("utf-8", errors="replace")
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    if content_type.partition(";")[0].strip().lower() == "multipart/form-data":
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + raw_body
        )
        if message.is_multipart():
            fields = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name and part.get_filename() is None and not part.is_multipart():
                    fields[name] = part.get_payload(decode=True).decode("utf-8", errors="replace")
            return fields
    return {k: v[0] for k, v in parse_qs(text).items()}


def _body_preview(raw_body: bytes) -> str:
    text = unquote_plus(raw_body.decode("utf-8", errors="replace"))
    # Normalize escaped JSON keys before looking for credentials.
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False)
    except ValueError:
        pass
    token = re.search("token", text, flags=re.IGNORECASE)
    if token:
        text = text[:token.start()] + "token=[redacted]"
    return ascii(text[:200])[1:-1][:200]


def _extract_webhook_fields(
    data: dict[str, Any],
) -> tuple[Optional[int], Optional[str], Optional[str]]:
    data = {key.strip(): value for key, value in data.items()}
    raw_event = data.get("event") or data.get("trigger")
    event = str(raw_event).strip()[:64] if raw_event else None

    site: Optional[str] = None
    for s_key in _SITE_KEYS:
        raw_site = data.get(s_key)
        if raw_site is not None and str(raw_site).strip():
            site = str(raw_site).strip()
            break

    order_id: Optional[int] = None
    for key in _ORDER_ID_KEYS:
        order_id = _parse_positive_int(data.get(key))
        if order_id is not None:
            break

    nested = data.get("order")
    if isinstance(nested, str) and nested.lstrip().startswith("{"):
        try:
            parsed_nested = json.loads(nested)
            if isinstance(parsed_nested, dict):
                nested = parsed_nested
        except ValueError:
            nested = None

    if isinstance(nested, dict):
        if order_id is None:
            order_id = _parse_positive_int(nested.get("id"))
        if site is None and nested.get("site"):
            site = str(nested["site"]).strip() or None

    return order_id, event, site


@router.api_route(
    "/retailcrm",
    methods=["GET", "POST"],
    dependencies=[Depends(require_retailcrm_webhook_token)],
)
async def retailcrm_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    crm_factory: Callable[[RuntimeConfig], RetailCRMClient] = Depends(get_retailcrm_factory),
    komtet_factory: Callable[[RuntimeConfig], KomtetClient] = Depends(get_komtet_factory),
    app_settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Handle RetailCRM trigger webhook (GET/POST; query, JSON or form-urlencoded)."""
    started_at = time.perf_counter()
    client_ip = get_client_ip(request)
    order_id: Optional[int] = None
    trigger_event: Optional[str] = None
    webhook_site: Optional[str] = None

    query_params = dict(request.query_params)
    if query_params:
        order_id, trigger_event, webhook_site = _extract_webhook_fields(query_params)

    raw_body = await request.body() if request.method == "POST" else b""
    content_type = request.headers.get("content-type", "")
    if raw_body and (order_id is None or trigger_event is None or webhook_site is None):
        fields = _body_fields(raw_body, content_type)
        body_id, body_event, body_site = _extract_webhook_fields(fields)
        order_id = order_id or body_id
        trigger_event = trigger_event or body_event
        webhook_site = webhook_site or body_site

    if order_id is None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.warning(
            "[Webhook/RetailCRM] incoming method=%s ip=%s order_id=None site=%s event=%s "
            "-> status=invalid reason=missing_order_id http_status=422 content_type=%r body_preview=%s (%.0f ms)",
            request.method,
            client_ip,
            webhook_site or "-",
            trigger_event or "webhook",
            content_type,
            _body_preview(raw_body),
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Missing or invalid 'order_id' in RetailCRM webhook request",
        )

    logger.info(
        "[Webhook/RetailCRM] incoming method=%s ip=%s order_id=%s site=%s event=%s",
        request.method,
        client_ip,
        order_id,
        webhook_site or "-",
        trigger_event or "webhook",
    )

    cfg = await load_runtime_config(db, env_settings=app_settings)
    try:
        async with crm_factory(cfg) as crm, komtet_factory(cfg) as komtet:
            result = await sync_retailcrm_to_komtet(
                db,
                order_id=order_id,
                trigger_event=trigger_event or "webhook",
                site=webhook_site,
                crm_client=crm,
                komtet_client=komtet,
            )
    except ValueError as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.error(
            "[Webhook/RetailCRM] done order_id=%s status=error reason=credentials_not_configured "
            "http_status=503 (%.0f ms)",
            order_id,
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Integration credentials are not configured: {exc}",
        ) from exc

    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    if result.get("status") == "error" and result.get("transient_error") is True:
        logger.warning(
            "[Webhook/RetailCRM] done order_id=%s status=%s reason=%s http_status=503 (%.0f ms)",
            order_id,
            result.get("status"),
            result.get("reason") or "transient_error",
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result.get("error") or "Transient synchronization error",
        )

    logger.info(
        "[Webhook/RetailCRM] done order_id=%s status=%s reason=%s http_status=200 (%.0f ms)",
        order_id,
        result.get("status"),
        result.get("reason") or "-",
        elapsed_ms,
    )
    return {"ok": True, "result": result}


def _first_header_value(request: Request, name: str) -> str:
    """Return the first (client-closest proxy) value of a comma-separated forwarding header."""
    raw = request.headers.get(name, "")
    return raw.split(",")[0].strip() if raw else ""


def _strip_default_port(host: str, proto: str) -> str:
    """Remove explicit default port (`:443` for https, `:80` for http) from a Host value."""
    default_port = {"http": "80", "https": "443"}.get(proto.lower())
    if default_port and not host.endswith("]") and host.endswith(f":{default_port}"):
        return host[: -(len(default_port) + 1)]
    return host


def _build_candidate_urls(request: Request) -> list[str]:
    """Build URLs KOMTET may have signed, accounting for TLS-terminating reverse proxies.

    The authoritative candidate is the configured `komtet_callback_url` (added by the
    sync service); these are fallbacks reconstructed from `X-Forwarded-*` headers.
    Spoofed headers cannot bypass verification because the HMAC requires the secret.
    """
    urls: list[str] = [str(request.url)]
    query = f"?{request.url.query}" if request.url.query else ""
    path = request.url.path

    proto = (_first_header_value(request, "x-forwarded-proto") or request.url.scheme).lower()
    raw_host = _first_header_value(request, "x-forwarded-host") or request.headers.get("host", "")
    port = _first_header_value(request, "x-forwarded-port")
    prefix = _first_header_value(request, "x-forwarded-prefix").rstrip("/")

    if raw_host:
        default_port = {"http": "80", "https": "443"}.get(proto)
        host = _strip_default_port(raw_host, proto)
        host_has_port = ":" in host and host.rsplit(":", 1)[-1].isdigit() and not host.endswith("]")
        if port and not host_has_port and port != default_port:
            host = f"{host}:{port}"
        for candidate_path in ([f"{prefix}{path}", path] if prefix else [path]):
            candidate = f"{proto}://{host}{candidate_path}{query}"
            if candidate not in urls:
                urls.append(candidate)
    return urls


@router.post("/komtet")
async def komtet_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    crm_factory: Callable[[RuntimeConfig], RetailCRMClient] = Depends(get_retailcrm_factory),
    komtet_factory: Callable[[RuntimeConfig], KomtetClient] = Depends(get_komtet_factory),
    app_settings: Settings = Depends(get_app_settings),
) -> dict[str, Any]:
    """Handle incoming KOMTET Kassa order callback with X-HMAC-Signature verification."""
    started_at = time.perf_counter()
    client_ip = get_client_ip(request)
    raw_body = await request.body()
    signature = request.headers.get("X-HMAC-Signature", "")
    candidate_urls = _build_candidate_urls(request)
    query_string = request.url.query

    cfg = await load_runtime_config(db, env_settings=app_settings)
    hmac_checked = bool(cfg.verify_komtet_signature or app_settings.is_production)

    # Verify HMAC signature against raw_body BEFORE parsing JSON or creating downstream clients.
    try:
        await verify_komtet_callback_signature(
            db,
            cfg,
            payload=None,
            signature=signature,
            raw_url=str(request.url),
            candidate_urls=candidate_urls,
            raw_body=raw_body,
            query_string=query_string,
            require_signature=app_settings.is_production,
        )
    except PermissionError as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.warning(
            "[Webhook/KOMTET] ip=%s hmac=invalid komtet_id=- state=- -> status=rejected "
            "http_status=401 (%.0f ms)",
            client_ip,
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    hmac_status = "valid" if hmac_checked else "skipped"

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.warning(
            "[Webhook/KOMTET] ip=%s hmac=%s komtet_id=- state=- -> status=invalid_json "
            "http_status=400 (%.0f ms)",
            client_ip,
            hmac_status,
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    if not isinstance(payload, dict):
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.warning(
            "[Webhook/KOMTET] ip=%s hmac=%s komtet_id=- state=- -> status=invalid_json "
            "http_status=400 (%.0f ms)",
            client_ip,
            hmac_status,
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Callback payload must be a JSON object",
        )

    cb_komtet_id = payload.get("id")
    cb_komtet_id_log = cb_komtet_id if cb_komtet_id is not None else "-"
    cb_state = str(payload.get("state") or "").strip().lower() or "-"
    logger.info(
        "[Webhook/KOMTET] incoming ip=%s hmac=%s komtet_id=%s state=%s",
        client_ip,
        hmac_status,
        cb_komtet_id_log,
        cb_state,
    )

    try:
        async with crm_factory(cfg) as crm:
            result = await sync_komtet_to_retailcrm(
                db,
                payload,
                signature=signature,
                raw_url=str(request.url),
                candidate_urls=candidate_urls,
                raw_body=raw_body,
                query_string=query_string,
                crm_client=crm,
                require_signature=app_settings.is_production,
                signature_already_verified=True,
                komtet_factory=komtet_factory,
            )
    except ValueError as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        logger.error(
            "[Webhook/KOMTET] done ip=%s hmac=%s komtet_id=%s state=%s status=error "
            "reason=credentials_not_configured http_status=503 (%.0f ms)",
            client_ip,
            hmac_status,
            cb_komtet_id_log,
            cb_state,
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Integration credentials are not configured: {exc}",
        ) from exc

    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    if result.get("status") == "error" and result.get("transient_error") is True:
        logger.warning(
            "[Webhook/KOMTET] done ip=%s hmac=%s komtet_id=%s state=%s status=%s reason=%s "
            "http_status=503 (%.0f ms)",
            client_ip,
            hmac_status,
            cb_komtet_id_log,
            cb_state,
            result.get("status"),
            result.get("reason") or "transient_error",
            elapsed_ms,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result.get("error") or "Transient synchronization error",
        )

    logger.info(
        "[Webhook/KOMTET] done ip=%s hmac=%s komtet_id=%s state=%s status=%s reason=%s "
        "http_status=200 (%.0f ms)",
        client_ip,
        hmac_status,
        cb_komtet_id_log,
        cb_state,
        result.get("status"),
        result.get("reason") or "-",
        elapsed_ms,
    )
    return {"ok": True, "result": result}
