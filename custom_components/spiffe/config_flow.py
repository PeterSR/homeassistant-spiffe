"""Config flow for the SPIFFE integration."""

from __future__ import annotations

import logging
import os
import re
import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    CALL_TIMEOUT,
    CONF_ALLOWED_CALLERS,
    CONF_EXPOSE_WHOAMI,
    CONF_NAME,
    CONF_SINGLE_USE_TOKENS,
    CONF_SOCKET,
    CONF_SPIFFE_ID,
    CONF_WEBHOOK_ID,
    DEFAULT_SOCKET,
    DOMAIN,
    SUBENTRY_EVENT_WEBHOOK,
)
from .ids import normalize_spiffe_id
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


_WEBHOOK_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")


class SpiffeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Workload API address and check that it answers."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SpiffeOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_EVENT_WEBHOOK: EventWebhookSubentryFlow}

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
            errors["base"] = "no_identity" if err.permission_denied else "cannot_connect"
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


class SpiffeOptionsFlow(OptionsFlow):
    """Home Assistant's own SPIFFE ID, and whether to offer the whoami endpoint."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            spiffe_id = user_input.get(CONF_SPIFFE_ID, "").strip()
            result: dict[str, Any] = {
                CONF_EXPOSE_WHOAMI: user_input[CONF_EXPOSE_WHOAMI],
                CONF_SINGLE_USE_TOKENS: user_input[CONF_SINGLE_USE_TOKENS],
            }
            if spiffe_id:
                try:
                    result[CONF_SPIFFE_ID] = normalize_spiffe_id(spiffe_id)
                except ValueError:
                    errors[CONF_SPIFFE_ID] = "invalid_spiffe_id"
            if not errors:
                return self.async_create_entry(data=result)
        current = user_input if user_input is not None else options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SPIFFE_ID, description={"suggested_value": current.get(CONF_SPIFFE_ID)}
                ): str,
                vol.Required(
                    CONF_EXPOSE_WHOAMI, default=bool(current.get(CONF_EXPOSE_WHOAMI, False))
                ): BooleanSelector(),
                vol.Required(
                    CONF_SINGLE_USE_TOKENS,
                    default=bool(current.get(CONF_SINGLE_USE_TOKENS, False)),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)


class EventWebhookSubentryFlow(ConfigSubentryFlow):
    """A webhook that turns verified requests into `spiffe_webhook` events."""

    def _used_webhook_ids(self, *, except_id: str | None = None) -> set[str]:
        entry = self._get_entry()
        return {
            sub.data[CONF_WEBHOOK_ID]
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_EVENT_WEBHOOK and sub.subentry_id != except_id
        }

    def _used_names(self, *, except_id: str | None = None) -> set[str]:
        entry = self._get_entry()
        return {
            sub.data[CONF_NAME].casefold()
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_EVENT_WEBHOOK and sub.subentry_id != except_id
        }

    def _validate(
        self, user_input: dict[str, Any], *, except_id: str | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        errors: dict[str, str] = {}
        name = user_input[CONF_NAME].strip()
        webhook_id = user_input[CONF_WEBHOOK_ID].strip()
        if not name:
            errors[CONF_NAME] = "name_required"
        elif name.casefold() in self._used_names(except_id=except_id):
            errors[CONF_NAME] = "name_in_use"
        if not _WEBHOOK_ID.fullmatch(webhook_id):
            errors[CONF_WEBHOOK_ID] = "invalid_webhook_id"
        elif webhook_id in self._used_webhook_ids(except_id=except_id):
            errors[CONF_WEBHOOK_ID] = "webhook_id_in_use"
        callers: list[str] = []
        try:
            for caller in user_input.get(CONF_ALLOWED_CALLERS, []):
                normalized = normalize_spiffe_id(caller)
                if normalized not in callers:
                    callers.append(normalized)
        except ValueError:
            errors[CONF_ALLOWED_CALLERS] = "invalid_spiffe_id"
        else:
            if not callers:
                errors[CONF_ALLOWED_CALLERS] = "callers_required"
        return {
            CONF_NAME: name,
            CONF_WEBHOOK_ID: webhook_id,
            CONF_ALLOWED_CALLERS: callers,
        }, errors

    @staticmethod
    def _schema(values: dict[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default=values.get(CONF_NAME, "")): str,
                vol.Required(CONF_WEBHOOK_ID, default=values.get(CONF_WEBHOOK_ID, "")): str,
                vol.Required(
                    CONF_ALLOWED_CALLERS, default=values.get(CONF_ALLOWED_CALLERS, [])
                ): SelectSelector(
                    SelectSelectorConfig(options=[], multiple=True, custom_value=True)
                ),
            }
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        values: dict[str, Any] = {CONF_WEBHOOK_ID: secrets.token_urlsafe(24)}
        if user_input is not None:
            data, errors = self._validate(user_input)
            if not errors:
                return self.async_create_entry(title=data[CONF_NAME], data=data)
            values = user_input
        return self.async_show_form(step_id="user", data_schema=self._schema(values), errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        values: dict[str, Any] = dict(subentry.data)
        if user_input is not None:
            data, errors = self._validate(user_input, except_id=subentry.subentry_id)
            if not errors:
                return self.async_update_and_abort(
                    entry, subentry, title=data[CONF_NAME], data=data
                )
            values = user_input
        return self.async_show_form(
            step_id="reconfigure", data_schema=self._schema(values), errors=errors
        )
