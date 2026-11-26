"""Async RetailCRM API v5 client built on httpx."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.3, 0.7, 1.5)


def _truncate_error(text: str, limit: int = 500) -> str:
    """Collapse whitespace and truncate error details for safe logging."""
    return " ".join((text or "").split())[:limit]


class RetailCRMError(Exception):
    """Raised when RetailCRM API returns an error or HTTP/network failure occurs."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        errors: Any = None,
        response_data: Optional[dict[str, Any]] = None,
        is_transient: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.errors = errors
        self.response_data = response_data
        if is_transient is None:
            self.is_transient = status_code is None or status_code in TRANSIENT_HTTP_STATUSES
        else:
            self.is_transient = is_transient

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"(HTTP {self.status_code})")
        if self.errors:
            parts.append(f"errors={self.errors}")
        return " ".join(parts)


def _flatten_query_params(
    params: dict[str, Any],
    prefix: str = "",
) -> list[tuple[str, str]]:
    """Flatten nested dicts/lists into Symfony-compatible bracket query parameters."""
    flattened: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        full_key = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            flattened.extend(_flatten_query_params(value, full_key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                if item is None:
                    continue
                if isinstance(item, dict):
                    flattened.extend(_flatten_query_params(item, f"{full_key}[]"))
                elif isinstance(item, bool):
                    flattened.append((f"{full_key}[]", "1" if item else "0"))
                else:
                    flattened.append((f"{full_key}[]", str(item)))
        elif isinstance(value, bool):
            flattened.append((full_key, "1" if value else "0"))
        else:
            flattened.append((full_key, str(value)))
    return flattened


class RetailCRMClient:
    """Async HTTP client for RetailCRM API v5."""

    API_VERSION = "v5"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url:
            raise ValueError("RetailCRM base_url must not be empty")
        if not api_key or not api_key.strip():
            raise ValueError("RetailCRM api_key must not be empty")

        self._base_url = normalized_url
        self._api_key = api_key.strip()
        self._retry_delays = tuple(retry_delays)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> RetailCRMClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying httpx client if owned by this instance."""
        if self._owns_client:
            await self._client.aclose()

    def _build_url(self, path: str, *, versioned: bool = True) -> str:
        clean_path = "/" + path.lstrip("/")
        if versioned:
            return f"{self._base_url}/api/{self.API_VERSION}{clean_path}"
        return f"{self._base_url}/api{clean_path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        versioned: bool = True,
        params: Optional[dict[str, Any]] = None,
        form_data: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        upper_method = method.upper()
        clean_path = "/" + path.lstrip("/")
        safe_path = (
            f"/api/{self.API_VERSION}{clean_path}" if versioned else f"/api{clean_path}"
        ).split("?", 1)[0]
        url = self._build_url(path, versioned=versioned)
        headers = {
            "X-API-KEY": self._api_key,
            "Accept": "application/json",
        }
        query_params = _flatten_query_params(params) if params else None

        response: Optional[httpx.Response] = None
        total_retries = len(self._retry_delays)
        max_attempts = total_retries + 1
        started_at = time.perf_counter()

        for attempt in range(max_attempts):
            try:
                response = await self._client.request(
                    method=upper_method,
                    url=url,
                    headers=headers,
                    params=query_params,
                    data=form_data,
                )
            except httpx.TransportError as exc:
                if attempt < total_retries:
                    delay = self._retry_delays[attempt]
                    logger.warning(
                        "[RetailCRM] retry %d/%d after %s %s -> %s (delay=%.1fs)",
                        attempt + 1,
                        total_retries,
                        upper_method,
                        safe_path,
                        type(exc).__name__,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                logger.error(
                    "[RetailCRM] %s %s -> %s (%.0f ms): %s",
                    upper_method,
                    safe_path,
                    type(exc).__name__,
                    elapsed_ms,
                    _truncate_error(str(exc)),
                )
                raise RetailCRMError(
                    f"RetailCRM transport error: {exc}",
                    is_transient=True,
                ) from exc
            except httpx.HTTPError as exc:
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                logger.error(
                    "[RetailCRM] %s %s -> %s (%.0f ms): %s",
                    upper_method,
                    safe_path,
                    type(exc).__name__,
                    elapsed_ms,
                    _truncate_error(str(exc)),
                )
                raise RetailCRMError(
                    f"RetailCRM HTTP error: {exc}",
                    is_transient=True,
                ) from exc

            if response.status_code in RETRYABLE_HTTP_STATUSES and attempt < total_retries:
                delay = self._retry_delays[attempt]
                logger.warning(
                    "[RetailCRM] retry %d/%d after %s %s -> %s (delay=%.1fs)",
                    attempt + 1,
                    total_retries,
                    upper_method,
                    safe_path,
                    response.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            break

        assert response is not None
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error(
                "[RetailCRM] %s %s -> %s (%.0f ms): %s",
                upper_method,
                safe_path,
                response.status_code,
                elapsed_ms,
                _truncate_error(f"non-JSON response: {response.text}"),
            )
            raise RetailCRMError(
                f"RetailCRM returned non-JSON response: {response.text[:500]}",
                status_code=response.status_code,
            ) from exc

        if not isinstance(payload, dict):
            logger.error(
                "[RetailCRM] %s %s -> %s (%.0f ms): %s",
                upper_method,
                safe_path,
                response.status_code,
                elapsed_ms,
                f"unexpected JSON payload type: {type(payload).__name__}",
            )
            raise RetailCRMError(
                "RetailCRM returned unexpected JSON payload type",
                status_code=response.status_code,
            )

        if response.status_code >= 400 or payload.get("success") is False:
            error_msg = payload.get("errorMsg") or f"RetailCRM API error on {method} {path}"
            err_parts: list[str] = []
            if payload.get("errorMsg"):
                err_parts.append(str(payload["errorMsg"]))
            if payload.get("errors"):
                err_parts.append(f"errors={json.dumps(payload['errors'], ensure_ascii=False)}")
            raw_err_body = "; ".join(err_parts) if err_parts else response.text
            logger.error(
                "[RetailCRM] %s %s -> %s (%.0f ms): %s",
                upper_method,
                safe_path,
                response.status_code,
                elapsed_ms,
                _truncate_error(raw_err_body),
            )
            raise RetailCRMError(
                error_msg,
                status_code=response.status_code,
                errors=payload.get("errors"),
                response_data=payload,
            )

        logger.info(
            "[RetailCRM] %s %s -> %s (%.0f ms)",
            upper_method,
            safe_path,
            response.status_code,
            elapsed_ms,
        )
        return payload

    # ------------------------------------------------------------------
    # Authentication & System
    # ------------------------------------------------------------------

    async def check_credentials(self) -> dict[str, Any]:
        """Verify API key validity and retrieve granted scopes (`GET /api/credentials`)."""
        return await self._request("GET", "/credentials", versioned=False)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def get_order(
        self,
        order_id: int | str,
        *,
        by: Literal["id", "externalId"] = "id",
        site: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch a single order by internal `id` or `externalId`."""
        params: dict[str, Any] = {}
        if by != "externalId":
            params["by"] = by
        if site is not None:
            params["site"] = site
        encoded_id = quote(str(order_id), safe="")
        return await self._request("GET", f"/orders/{encoded_id}", params=params)

    async def get_orders(
        self,
        *,
        filters: Optional[dict[str, Any]] = None,
        limit: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        """List orders matching optional filters."""
        params: dict[str, Any] = {"limit": limit, "page": page}
        if filters:
            params["filter"] = filters
        return await self._request("GET", "/orders", params=params)

    async def get_offers(self, ids: list[int], site: Optional[str]) -> dict[str, Any]:
        filters: dict[str, Any] = {"ids": ids}
        if site:
            filters["sites"] = [site]
        return await self._request("GET", "/store/offers", params={"filter": filters, "limit": 100})

    async def edit_order(
        self,
        order_id: int | str,
        order_data: dict[str, Any],
        *,
        by: Literal["id", "externalId"] = "id",
        site: Optional[str] = None,
    ) -> dict[str, Any]:
        """Edit an existing order (`POST /api/v5/orders/{uid}/edit`)."""
        form_data: dict[str, str] = {
            "order": json.dumps(order_data, ensure_ascii=False),
        }
        if by != "externalId":
            form_data["by"] = by
        if site is not None:
            form_data["site"] = site
        encoded_id = quote(str(order_id), safe="")
        return await self._request("POST", f"/orders/{encoded_id}/edit", form_data=form_data)

    # ------------------------------------------------------------------
    # Order Payments
    # ------------------------------------------------------------------

    async def create_payment(
        self,
        payment_data: dict[str, Any],
        *,
        site: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a payment linked to an order (`POST /api/v5/orders/payments/create`)."""
        form_data: dict[str, str] = {
            "payment": json.dumps(payment_data, ensure_ascii=False),
        }
        if site is not None:
            form_data["site"] = site
        return await self._request("POST", "/orders/payments/create", form_data=form_data)

    async def update_payment(
        self,
        payment_id: int | str,
        payment_data: dict[str, Any],
        *,
        by: Literal["id", "externalId"] = "id",
        site: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update an existing order payment (`POST /api/v5/orders/payments/{uid}/edit`)."""
        form_data: dict[str, str] = {
            "payment": json.dumps(payment_data, ensure_ascii=False),
        }
        if by != "externalId":
            form_data["by"] = by
        if site is not None:
            form_data["site"] = site
        encoded_id = quote(str(payment_id), safe="")
        return await self._request(
            "POST", f"/orders/payments/{encoded_id}/edit", form_data=form_data
        )

    async def delete_payment(self, payment_id: int) -> dict[str, Any]:
        """Delete an order payment by its internal ID."""
        return await self._request(
            "POST", f"/orders/payments/{payment_id}/delete", form_data={"by": "id"}
        )

    # ------------------------------------------------------------------
    # Reference Dictionaries
    # ------------------------------------------------------------------

    async def get_statuses(self) -> dict[str, Any]:
        """Fetch order statuses dictionary (`GET /api/v5/reference/statuses`)."""
        return await self._request("GET", "/reference/statuses")

    async def get_payment_types(self) -> dict[str, Any]:
        """Fetch payment types dictionary (`GET /api/v5/reference/payment-types`)."""
        return await self._request("GET", "/reference/payment-types")

    async def get_couriers(self) -> dict[str, Any]:
        """Fetch couriers dictionary (`GET /api/v5/reference/couriers`)."""
        return await self._request("GET", "/reference/couriers")

    async def get_sites(self) -> dict[str, Any]:
        """Fetch sites dictionary (`GET /api/v5/reference/sites`)."""
        return await self._request("GET", "/reference/sites")

    async def get_delivery_types(self) -> dict[str, Any]:
        """Fetch delivery types dictionary (`GET /api/v5/reference/delivery-types`)."""
        return await self._request("GET", "/reference/delivery-types")

    # ------------------------------------------------------------------
    # Custom Fields
    # ------------------------------------------------------------------

    async def get_custom_fields(
        self,
        *,
        entity: Optional[str] = None,
        limit: int = 100,
        page: int = 1,
    ) -> dict[str, Any]:
        """List custom fields (`GET /api/v5/custom-fields`)."""
        params: dict[str, Any] = {"limit": limit, "page": page}
        if entity is not None:
            params["filter"] = {"entity": entity}
        return await self._request("GET", "/custom-fields", params=params)

    async def get_custom_field(
        self,
        code: str,
        *,
        entity: str = "order",
    ) -> Optional[dict[str, Any]]:
        """Get a single custom field definition; returns None if not found (404)."""
        encoded_entity = quote(entity, safe="")
        encoded_code = quote(code, safe="")
        try:
            result = await self._request(
                "GET", f"/custom-fields/{encoded_entity}/{encoded_code}"
            )
            return result.get("customField")
        except RetailCRMError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def create_custom_field(
        self,
        code: str,
        name: str,
        *,
        field_type: str = "string",
        entity: str = "order",
        required: bool = False,
        in_filter: bool = True,
        in_list: bool = True,
        view_mode: str = "editable",
        display_area: Optional[str] = None,
        ordering: Optional[int] = None,
    ) -> dict[str, Any]:
        """Create a custom field (`POST /api/v5/custom-fields/{entity}/create`)."""
        custom_field_payload: dict[str, Any] = {
            "code": code,
            "name": name,
            "type": field_type,
            "entity": entity,
            "required": required,
            "inFilter": in_filter,
            "inList": in_list,
            "viewMode": view_mode,
        }
        if display_area is not None:
            custom_field_payload["displayArea"] = display_area
        if ordering is not None:
            custom_field_payload["ordering"] = ordering

        encoded_entity = quote(entity, safe="")
        form_data = {
            "customField": json.dumps(custom_field_payload, ensure_ascii=False),
        }
        return await self._request(
            "POST", f"/custom-fields/{encoded_entity}/create", form_data=form_data
        )

    async def ensure_komtet_id_field(
        self,
        *,
        code: str = "komtet_id",
        name: str = "komtet_id",
        field_type: str = "integer",
        entity: str = "order",
    ) -> dict[str, Any]:
        """Ensure the `komtet_id` custom field exists on `order`; create it if absent."""
        existing = await self.get_custom_field(code, entity=entity)
        if existing is not None:
            return {"created": False, "customField": existing}

        created = await self.create_custom_field(
            code=code,
            name=name,
            field_type=field_type,
            entity=entity,
            required=False,
            in_filter=True,
            in_list=True,
            view_mode="editable",
        )
        return {"created": True, "response": created}
