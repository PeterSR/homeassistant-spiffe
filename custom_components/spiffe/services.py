"""The http_request action: call a URL with a JWT-SVID for an audience."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service import async_register_admin_service

from .api import async_fetch_jwt_svid
from .const import DOMAIN, RESPONSE_LIMIT_BYTES
from .ids import normalize_spiffe_id

_LOGGER = logging.getLogger(__name__)

SERVICE_HTTP_REQUEST = "http_request"
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def _spiffe_id(value: Any) -> str:
    try:
        return normalize_spiffe_id(value)
    except ValueError as err:
        raise vol.Invalid(f"not a valid SPIFFE ID: {err}") from err


HTTP_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required("url"): cv.string,
        vol.Required("audience"): _spiffe_id,
        vol.Optional("method", default="POST"): vol.All(vol.Upper, vol.In(METHODS)),
        vol.Optional("json"): dict,
        vol.Optional("headers"): vol.Schema({cv.string: cv.string}),
        vol.Optional("timeout", default=10): vol.All(vol.Coerce(float), vol.Range(min=1, max=120)),
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions. They need an administrator."""
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_HTTP_REQUEST,
        _async_http_request,
        schema=HTTP_REQUEST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_url")


# An HTTP header name is a token (RFC 9110); a value must not hold line breaks or NUL, which
# would let a caller smuggle in headers of their own.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HEADER_VALUE_FORBIDDEN = re.compile(r"[\r\n\x00]")


def _check_headers(headers: dict[str, str]) -> None:
    for name, value in headers.items():
        if not _HEADER_NAME.fullmatch(name) or _HEADER_VALUE_FORBIDDEN.search(value):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_header",
                translation_placeholders={"header": repr(name)},
            )
    if any(name.lower() == "authorization" for name in headers):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="authorization_header"
        )


async def _read_limited(response: aiohttp.ClientResponse, limit: int) -> tuple[bytes, bool]:
    """Read at most `limit` bytes. The flag says whether there was more."""
    chunks: list[bytes] = []
    size = 0
    stream = response.content
    while size <= limit:
        chunk = await stream.read(limit + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    body = b"".join(chunks)
    return body[:limit], len(body) > limit


async def _async_http_request(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    url: str = call.data["url"]
    headers: dict[str, str] = dict(call.data.get("headers", {}))
    _check_url(url)
    _check_headers(headers)

    token = await async_fetch_jwt_svid(hass, call.data["audience"])
    headers["Authorization"] = f"Bearer {token}"
    session = async_get_clientsession(hass)
    try:
        # Redirects are not followed: the token is for the URL that was asked for, not for
        # wherever that server sends us.
        async with session.request(
            call.data["method"],
            url,
            json=call.data.get("json"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=call.data["timeout"]),
            allow_redirects=False,
        ) as response:
            body, truncated = await _read_limited(response, RESPONSE_LIMIT_BYTES)
            status = response.status
            content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
    except (aiohttp.ClientError, TimeoutError) as err:
        # Only the host and the kind of error: a URL can hold credentials, which a message must not.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="request_failed",
            translation_placeholders={
                "host": urlsplit(url).hostname or "",
                "error": type(err).__name__,
            },
        ) from err

    if not truncated and content_type.endswith("json"):
        try:
            return {"status": status, "json": json.loads(body)}
        except ValueError:
            pass
    return {"status": status, "text": body.decode("utf-8", errors="replace")}
