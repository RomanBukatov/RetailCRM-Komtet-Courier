"""Authentication dependencies for the admin API and inbound RetailCRM webhooks."""

from __future__ import annotations

import hmac
import ipaddress
import logging
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_FORBIDDEN_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_FORBIDDEN_SUFFIXES = (".localhost", ".local", ".internal")


def validate_outbound_url(
    raw_url: str,
    *,
    allow_http: bool = False,
    field_name: str = "url",
) -> str:
    """Validate an outbound service URL against SSRF and insecure schemes.

    Allows only `https://` (`http://` is permitted strictly in `test` environments)
    and rejects localhost, loopback, private, link-local, reserved, and unspecified IPs.
    """
    cleaned = str(raw_url or "").strip().rstrip("/")
    if not cleaned:
        return ""

    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if scheme not in allowed_schemes:
        allowed_desc = "https:// or http://" if allow_http else "https://"
        raise ValueError(f"{field_name} must use {allowed_desc} scheme")

    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not contain embedded credentials")

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ValueError(f"{field_name} must contain a valid hostname")

    if hostname in _FORBIDDEN_HOSTNAMES or hostname.endswith(_FORBIDDEN_SUFFIXES):
        raise ValueError(f"{field_name} host '{hostname}' is not allowed")

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    if ip is not None and (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(f"{field_name} IP '{hostname}' is in a forbidden network range")

    return cleaned


def get_client_ip(request: Request) -> str:
    """Extract the client IP from reverse-proxy headers or the socket peer address."""
    for header_name in ("x-real-ip", "x-forwarded-for"):
        raw = request.headers.get(header_name, "").strip()
        if raw:
            first = raw.split(",")[0].strip()
            if first:
                return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def get_app_settings(request: Request) -> Settings:
    """Return Settings bound to the running app (falls back to cached env settings)."""
    settings = getattr(request.app.state, "settings", None)
    return settings if isinstance(settings, Settings) else get_settings()


def _tokens_match(expected: str, provided: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))


def _extract_bearer(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header[:7].lower() == "bearer ":
        return auth_header[7:].strip()
    return request.headers.get("x-admin-token", "").strip()


async def require_admin_token(request: Request) -> None:
    """Protect admin endpoints with `Authorization: Bearer <ADMIN_API_TOKEN>`.

    Fails closed in production when the token is not configured.
    """
    settings = get_app_settings(request)
    expected = settings.admin_api_token.get_secret_value()
    if not expected:
        if settings.is_production:
            logger.error("ADMIN_API_TOKEN is not configured; admin API is disabled in production")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin API token is not configured on the server",
            )
        return

    if not _tokens_match(expected, _extract_bearer(request)):
        logger.warning(
            "[Auth] Rejected admin request %s %s from ip=%s",
            request.method,
            request.url.path,
            get_client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_retailcrm_webhook_token(request: Request) -> None:
    """Protect the RetailCRM trigger webhook with a shared secret.

    The token may be passed as `?token=...` (convenient for RetailCRM trigger URLs)
    or via the `X-Webhook-Token` header. Fails closed in production when unset.
    """
    settings = get_app_settings(request)
    expected = settings.retailcrm_webhook_token.get_secret_value()
    if not expected:
        if settings.is_production:
            logger.error(
                "RETAILCRM_WEBHOOK_TOKEN is not configured; RetailCRM webhook is disabled in production"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="RetailCRM webhook token is not configured on the server",
            )
        return

    provided = (
        request.headers.get("x-webhook-token", "").strip()
        or str(request.query_params.get("token") or "").strip()
    )
    if not _tokens_match(expected, provided):
        logger.warning(
            "[Auth] Rejected RetailCRM webhook from ip=%s",
            get_client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook token",
        )
