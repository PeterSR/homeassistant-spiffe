"""An opt-in endpoint that tells a caller how Home Assistant sees it."""

from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .api import _async_verify, error_response, warn_throttled
from .const import CONF_EXPOSE_WHOAMI, CONF_SPIFFE_ID, DOMAIN, WHOAMI_PATH
from .errors import SpiffeAuthError, SpiffeError, SpiffeNotReady

_LOGGER = logging.getLogger(__name__)


class WhoamiView(HomeAssistantView):
    """POST a JWT-SVID, get back the SPIFFE ID it proves. Answers 404 unless enabled."""

    url = WHOAMI_PATH
    name = "api:spiffe:whoami"
    requires_auth = False

    # Every method answers 404 while disabled, so the endpoint cannot be told apart from a path
    # that does not exist.
    async def get(self, request: web.Request) -> web.Response:
        return await self._handle(request)

    async def put(self, request: web.Request) -> web.Response:
        return await self._handle(request)

    async def patch(self, request: web.Request) -> web.Response:
        return await self._handle(request)

    async def delete(self, request: web.Request) -> web.Response:
        return await self._handle(request)

    async def head(self, request: web.Request) -> web.Response:
        return await self._handle(request)

    async def post(self, request: web.Request) -> web.Response:
        return await self._handle(request)

    async def _handle(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries or not entries[0].options.get(CONF_EXPOSE_WHOAMI):
            return web.Response(status=404)
        if request.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST"})
        audience = entries[0].options.get(CONF_SPIFFE_ID)
        try:
            caller = await _async_verify(hass, request, None, consume=False)
        except SpiffeAuthError as err:
            _LOGGER.info("whoami refused a request (%s): %s", err.status, err)
            return error_response(err.status)
        except (SpiffeError, SpiffeNotReady) as err:
            warn_throttled(hass, f"whoami: {err}", "whoami cannot verify requests: %s", err)
            return error_response(503)
        return self.json({"caller": caller, "audience": audience})
