"""Async KOMTET Kassa API v1/v2 client built on httpx.

Protocol matches the official `Komtet/komtet-kassa-python-sdk`:
- Base URL: `https://kassa.komtet.ru`
- Headers:
  - `Authorization`: `<shop_id>`
  - `Accept`: `application/json`
  - `Content-Type`: `application/json` (for POST/PUT requests with JSON body)
  - `X-HMAC-Signature`: lowercase hex HMAC-MD5 of `METHOD + FULL_URL + BODY`
    using `secret_key` as the HMAC key.
- Official SDK endpoints under `/api/shop/{v1|v2}/`:
  - `GET /api/shop/{v}/queues/{queue_id}`
  - `GET /api/shop/{v}/employees` (with `type=courier` for couriers)
  - `GET /api/shop/{v}/employees/{employee_id}`
  - `GET /api/shop/{v}/orders`
  - `GET /api/shop/{v}/orders/{order_id}`
  - `POST /api/shop/{v}/orders`
  - `PUT /api/shop/{v}/orders/{order_id}`
  - `DELETE /api/shop/{v}/orders/{order_id}`
"""

from __future__ import annotations

import asyncio
import decimal
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Literal, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_KOMTET_BASE_URL = "https://kassa.komtet.ru"
ApiVersion = Literal["v1", "v2"]
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRYABLE_HTTP_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_RETRY_DELAYS: tuple[float, ...] = (0.3, 0.7, 1.5)


def _truncate_error(text: str, limit: int = 500) -> str:
    """Collapse whitespace and truncate error details for safe logging."""
    return " ".join((text or "").split())[:limit]


class _DecimalJSONEncoder(json.JSONEncoder):
    """Serialize Decimal values as floats, matching official Komtet Python SDK."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)


class KomtetError(Exception):
    """Raised when KOMTET Kassa API returns a non-2xx response or network error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str | int] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        response_data: Any = None,
        is_transient: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.title = title
        self.description = description
        self.response_data = response_data
        if is_transient is None:
            self.is_transient = status_code is None or status_code in TRANSIENT_HTTP_STATUSES
        else:
            self.is_transient = is_transient

    def __str__(self) -> str:
        parts = [self.message]
        if self.status_code is not None:
            parts.append(f"(HTTP {self.status_code})")
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.description:
            parts.append(f"description={self.description}")
        return " ".join(parts)


class KomtetClient:
    """Async HTTP client for KOMTET Kassa Shop API (v1 and v2)."""

    def __init__(
        self,
        shop_id: str,
        secret_key: str,
        *,
        base_url: str = DEFAULT_KOMTET_BASE_URL,
        api_version: ApiVersion = "v2",
        timeout: float = 15.0,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        if not shop_id or not shop_id.strip():
            raise ValueError("KOMTET shop_id must not be empty")
        if not secret_key:
            raise ValueError("KOMTET secret_key must not be empty")
        normalized_url = (base_url or DEFAULT_KOMTET_BASE_URL).strip().rstrip("/")

        self._shop_id = shop_id.strip()
        self._secret_key = secret_key.encode("utf-8")
        self._base_url = normalized_url
        self._api_version: ApiVersion = api_version
        self._retry_delays = tuple(retry_delays)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> KomtetClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying httpx client if owned by this instance."""
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def compute_signature(
        secret_key: str | bytes,
        method: str,
        url: str,
        body: str = "",
    ) -> str:
        """Compute KOMTET HMAC-MD5 hex signature over `METHOD + URL + BODY`."""
        key_bytes = secret_key.encode("utf-8") if isinstance(secret_key, str) else secret_key
        payload = f"{method.upper()}{url}{body}".encode("utf-8")
        return hmac.new(key_bytes, payload, hashlib.md5).hexdigest()

    @classmethod
    def verify_signature(
        cls,
        secret_key: str | bytes,
        method: str,
        url: str,
        body: str,
        received_signature: str,
    ) -> bool:
        """Verify KOMTET `X-HMAC-Signature` using constant-time comparison."""
        if not received_signature:
            return False
        expected = cls.compute_signature(secret_key, method, url, body)
        return hmac.compare_digest(expected.lower(), received_signature.strip().lower())

    def _serialize_json(self, data: Any) -> str:
        return json.dumps(data, cls=_DecimalJSONEncoder)

    async def _request_absolute_path(
        self,
        method: str,
        full_api_path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_data: Any = None,
        retry: bool = True,
    ) -> Any:
        upper_method = method.upper()
        clean_path = "/" + full_api_path.lstrip("/")
        safe_path = clean_path.split("?", 1)[0]
        url = f"{self._base_url}{clean_path}"

        clean_params: Optional[dict[str, Any]] = None
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}

        body_str = ""
        content: Optional[bytes] = None
        if json_data is not None:
            body_str = self._serialize_json(json_data)
            content = body_str.encode("utf-8")

        # Build the httpx Request first so str(request.url) includes the exact
        # query-string encoding sent on the wire when computing the HMAC signature.
        request = self._client.build_request(
            method=upper_method,
            url=url,
            params=clean_params,
            content=content,
        )
        signature = self.compute_signature(
            self._secret_key,
            upper_method,
            str(request.url),
            body_str,
        )
        request.headers["Authorization"] = self._shop_id
        request.headers["Accept"] = "application/json"
        request.headers["X-HMAC-Signature"] = signature
        if upper_method in ("POST", "PUT", "PATCH") or content is not None:
            request.headers["Content-Type"] = "application/json"

        # Never follow redirects on mutating requests (POST/PUT/PATCH/DELETE):
        # redirects invalidate X-HMAC-Signature (bound to the original URL) and can leak Authorization.
        follow_redirects = upper_method == "GET"

        response: Optional[httpx.Response] = None
        total_retries = len(self._retry_delays) if retry else 0
        max_attempts = total_retries + 1
        started_at = time.perf_counter()

        for attempt in range(max_attempts):
            try:
                response = await self._client.send(request, follow_redirects=follow_redirects)
            except httpx.TransportError as exc:
                if attempt < total_retries and (
                    upper_method != "POST" or isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                ):
                    delay = self._retry_delays[attempt]
                    logger.warning(
                        "[KOMTET] retry %d/%d after %s %s -> %s (delay=%.1fs)",
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
                    "[KOMTET] %s %s -> %s (%.0f ms): %s",
                    upper_method,
                    safe_path,
                    type(exc).__name__,
                    elapsed_ms,
                    _truncate_error(str(exc)),
                )
                raise KomtetError(
                    f"KOMTET transport error: {exc}",
                    is_transient=True,
                ) from exc
            except httpx.HTTPError as exc:
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                logger.error(
                    "[KOMTET] %s %s -> %s (%.0f ms): %s",
                    upper_method,
                    safe_path,
                    type(exc).__name__,
                    elapsed_ms,
                    _truncate_error(str(exc)),
                )
                raise KomtetError(
                    f"KOMTET HTTP error: {exc}",
                    is_transient=True,
                ) from exc

            retryable_statuses = {429} if upper_method == "POST" else RETRYABLE_HTTP_STATUSES
            if response.status_code in retryable_statuses and attempt < total_retries:
                delay = self._retry_delays[attempt]
                logger.warning(
                    "[KOMTET] retry %d/%d after %s %s -> %s (delay=%.1fs)",
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

        parsed_body: Any = None
        if response.content:
            try:
                parsed_body = response.json()
            except ValueError:
                parsed_body = response.text

        if response.status_code >= 300:
            if isinstance(parsed_body, dict):
                title = parsed_body.get("title")
                code = parsed_body.get("code")
                description = parsed_body.get("description")
                msg = title or description or f"KOMTET API error on {upper_method} {clean_path}"
                err_parts = [
                    f"{k}={parsed_body[k]}"
                    for k in ("code", "title", "description", "error")
                    if parsed_body.get(k) not in (None, "")
                ]
                raw_err_body = ", ".join(err_parts) if err_parts else response.text
            else:
                title = None
                code = None
                description = response.text[:500] if response.text else None
                msg = f"KOMTET API error on {upper_method} {clean_path}"
                raw_err_body = response.text

            logger.error(
                "[KOMTET] %s %s -> %s (%.0f ms): %s",
                upper_method,
                safe_path,
                response.status_code,
                elapsed_ms,
                _truncate_error(raw_err_body),
            )
            raise KomtetError(
                msg,
                status_code=response.status_code,
                code=code,
                title=title,
                description=description,
                response_data=parsed_body,
            )

        logger.info(
            "[KOMTET] %s %s -> %s (%.0f ms)",
            upper_method,
            safe_path,
            response.status_code,
            elapsed_ms,
        )
        return parsed_body

    async def _request(
        self,
        method: str,
        resource_path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_data: Any = None,
        api_version: Optional[ApiVersion] = None,
        retry: bool = True,
    ) -> Any:
        version = api_version or self._api_version
        full_path = f"/api/shop/{version}/{resource_path.lstrip('/')}"
        return await self._request_absolute_path(
            method,
            full_path,
            params=params,
            json_data=json_data,
            retry=retry,
        )

    # ------------------------------------------------------------------
    # Queues
    # ------------------------------------------------------------------

    async def get_queue(
        self,
        queue_id: int | str,
        *,
        api_version: Optional[ApiVersion] = None,
    ) -> dict[str, Any]:
        """Get queue details (`GET /api/shop/{v}/queues/{queue_id}`)."""
        result = await self._request(
            "GET",
            f"queues/{quote(str(queue_id), safe='')}",
            api_version=api_version,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def is_queue_active(
        self,
        queue_id: int | str,
        *,
        api_version: Optional[ApiVersion] = None,
    ) -> bool:
        """Return True when queue `state == 'active'`."""
        info = await self.get_queue(queue_id, api_version=api_version)
        return info.get("state") == "active"

    # ------------------------------------------------------------------
    # Employees / Couriers
    # ------------------------------------------------------------------

    async def get_employees(
        self,
        *,
        employee_type: Optional[str] = None,
        start: int = 0,
        limit: int = 100,
        api_version: Optional[ApiVersion] = None,
    ) -> dict[str, Any]:
        """List employees (`GET /api/shop/{v}/employees`).

        Official KOMTET SDK uses `type='courier'` to retrieve couriers.
        """
        params: dict[str, Any] = {
            "start": str(start),
            "limit": str(limit),
        }
        if employee_type:
            params["type"] = employee_type
        result = await self._request(
            "GET",
            "employees",
            params=params,
            api_version=api_version,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def get_couriers(
        self,
        *,
        start: int = 0,
        limit: int = 100,
        api_version: Optional[ApiVersion] = None,
    ) -> dict[str, Any]:
        """List couriers via the official `employees?type=courier` endpoint."""
        return await self.get_employees(
            employee_type="courier",
            start=start,
            limit=limit,
            api_version=api_version,
        )

    async def get_employee(
        self,
        employee_id: int,
        *,
        api_version: Optional[ApiVersion] = None,
    ) -> dict[str, Any]:
        """Get single employee info (`GET /api/shop/{v}/employees/{employee_id}`)."""
        result = await self._request(
            "GET",
            f"employees/{quote(str(employee_id), safe='')}",
            api_version=api_version,
        )
        return result if isinstance(result, dict) else {"raw": result}

    # ------------------------------------------------------------------
    # Delivery Orders
    # ------------------------------------------------------------------

    async def get_orders(
        self,
        *,
        start: int = 0,
        limit: int = 20,
        courier_id: Optional[int | str] = None,
        date_start: Optional[str] = None,
        api_version: Optional[ApiVersion] = None,
    ) -> dict[str, Any]:
        """List delivery orders (`GET /api/shop/{v}/orders`)."""
        params: dict[str, Any] = {
            "start": str(start),
            "limit": str(limit),
        }
        if courier_id is not None:
            params["courier_id"] = str(courier_id)
        if date_start is not None:
            params["date_start"] = date_start
        result = await self._request(
            "GET",
            "orders",
            params=params,
            api_version=api_version,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def get_order(
        self,
        order_id: int | str,
        *,
        api_version: Optional[ApiVersion] = None,
    ) -> dict[str, Any]:
        """Get delivery order details (`GET /api/shop/{v}/orders/{order_id}`)."""
        result = await self._request(
            "GET",
            f"orders/{quote(str(order_id), safe='')}",
            api_version=api_version,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def create_order(
        self,
        order_payload: dict[str, Any],
        *,
        api_version: Optional[ApiVersion] = None,
    ) -> dict[str, Any]:
        """Create a delivery order (`POST /api/shop/{v}/orders`)."""
        result = await self._request(
            "POST",
            "orders",
            json_data=order_payload,
            api_version=api_version,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def update_order(
        self,
        order_id: int | str,
        order_payload: dict[str, Any],
        *,
        api_version: Optional[ApiVersion] = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        """Update an existing delivery order (`PUT /api/shop/{v}/orders/{order_id}`)."""
        result = await self._request(
            "PUT",
            f"orders/{quote(str(order_id), safe='')}",
            json_data=order_payload,
            api_version=api_version,
            retry=retry,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def delete_order(
        self,
        order_id: int | str,
        *,
        api_version: Optional[ApiVersion] = None,
    ) -> bool:
        """Delete a delivery order (`DELETE /api/shop/{v}/orders/{order_id}`)."""
        await self._request(
            "DELETE",
            f"orders/{quote(str(order_id), safe='')}",
            api_version=api_version,
        )
        return True

    async def get_raw(
        self,
        full_api_path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Execute a signed read-only GET request against an explicit API path."""
        return await self._request_absolute_path("GET", full_api_path, params=params)
