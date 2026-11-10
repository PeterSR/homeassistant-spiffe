"""Config flow for the SPIFFE integration."""

from __future__ import annotations

import logging
import os
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CALL_TIMEOUT, CONF_SOCKET, DEFAULT_SOCKET, DOMAIN
from .source import (
    InvalidSocketError,
    SocketMissingError,
    SourceUnavailableError,
    WorkloadApiSource,
)

_LOGGER = logging.getLogger(__name__)


def _check_connection(uri: str) -> None:
    """Connect and fetch the bundles once. Blocks; run in the executor."""
    source = WorkloadApiSource(uri, CALL_TIMEOUT)
    try:
        source.fetch_jwt_bundles()
    finally:
        source.close()


def _default_socket() -> str:
    # Set on Home Assistant Container or Core installs that run next to an agent. Home Assistant
    # OS cannot set Core's environment, so the documented default applies there.
    return os.environ.get("SPIFFE_ENDPOINT_SOCKET") or DEFAULT_SOCKET


class SpiffeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Workload API address and check that it answers."""

    VERSION = 1

    async def _async_validate(self, uri: str) -> dict[str, str]:
        errors: dict[str, str] = {}
        try:
            await self.hass.async_add_executor_job(_check_connection, uri)
        except InvalidSocketError as err:
            _LOGGER.debug("Invalid Workload API address %s: %s", uri, err)
            errors[CONF_SOCKET] = "invalid_socket"
        except SocketMissingError:
            errors[CONF_SOCKET] = "socket_missing"
        except SourceUnavailableError as err:
            _LOGGER.debug("Workload API at %s did not answer: %s", uri, err)
            errors["base"] = "cannot_connect"
        return errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            uri = user_input[CONF_SOCKET].strip()
            errors = await self._async_validate(uri)
            if not errors:
                return self.async_create_entry(title="SPIFFE Workload API", data={CONF_SOCKET: uri})
        suggested = user_input[CONF_SOCKET] if user_input else _default_socket()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_SOCKET, default=suggested): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            uri = user_input[CONF_SOCKET].strip()
            errors = await self._async_validate(uri)
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_SOCKET: uri})
        suggested = user_input[CONF_SOCKET] if user_input else entry.data[CONF_SOCKET]
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({vol.Required(CONF_SOCKET, default=suggested): str}),
            errors=errors,
        )
