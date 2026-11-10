"""Event webhooks: a verified request fires an event on the Home Assistant bus."""

from __future__ import annotations

import json
import logging
from functools import partial

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import async_register_verified_webhook
from .const import (
    CONF_ALLOWED_CALLERS,
    CONF_NAME,
    CONF_WEBHOOK_ID,
    DOMAIN,
    EVENT_BODY_LIMIT_BYTES,
    EVENT_WEBHOOK,
    SUBENTRY_EVENT_WEBHOOK,
)

_LOGGER = logging.getLogger(__name__)


def async_register_event_webhooks(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register a verified webhook for each event webhook subentry, until the entry unloads."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_EVENT_WEBHOOK:
            continue
        webhook_id: str = subentry.data[CONF_WEBHOOK_ID]
        name: str = subentry.data[CONF_NAME]
        try:
            async_register_verified_webhook(
                hass,
                DOMAIN,
                f"SPIFFE event: {name}",
                webhook_id,
                partial(_async_handle, name),
                allowed_callers=subentry.data[CONF_ALLOWED_CALLERS],
            )
        except ValueError:
            _LOGGER.error(
                "Cannot register the event webhook %s: its webhook ID is already in use", name
            )
            continue
        entry.async_on_unload(partial(webhook.async_unregister, hass, webhook_id))


async def _read_body(request: web.Request, limit: int) -> bytes | None:
    """Read the body, or return None when it is longer than `limit`.

    Reads through the content stream, which also exists on the request objects that cloud
    webhooks and the webhook/handle websocket command build, and stops after `limit` bytes.
    """
    length = getattr(request, "content_length", None)
    if length is not None and length > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    stream = request.content  # a property that builds a new reader on a MockRequest
    while size <= limit:
        chunk = await stream.read(limit + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return None if size > limit else b"".join(chunks)


async def _async_handle(
    name: str, hass: HomeAssistant, webhook_id: str, request: web.Request, caller: str
) -> web.Response:
    body = await _read_body(request, EVENT_BODY_LIMIT_BYTES)
    if body is None:
        return web.json_response({"error": "too_large"}, status=413)
    data = None
    if body.strip():
        try:
            data = json.loads(body)
        except ValueError, RecursionError:
            return web.json_response({"error": "invalid_json"}, status=400)
    hass.bus.async_fire(
        EVENT_WEBHOOK,
        {"webhook_id": webhook_id, "name": name, "caller": caller, "data": data},
    )
    return web.json_response({"ok": True})
