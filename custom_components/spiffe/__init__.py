"""Give Home Assistant a SPIFFE identity that other integrations can use.

The integration talks to a SPIFFE Workload API (any implementation: a SPIRE agent, spiffex, tbot,
or anything else that serves one) and offers other custom integrations a few helpers:

    from custom_components.spiffe import async_fetch_jwt_svid

    token = await async_fetch_jwt_svid(hass, "https://api.example.org")
    headers = {"Authorization": f"Bearer {token}"}

Add "spiffe" to "dependencies" in your manifest.json so it is set up before you. Tokens are short
lived: fetch one for each request, or at least each time you need one, instead of storing it.

To accept calls, register a webhook with `async_register_verified_webhook`, or check a request
yourself with `async_verify_request`. The names in `__all__` are the stable public API.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .api import (
    async_fetch_jwt_svid,
    async_get_jwt_bundles,
    async_register_verified_webhook,
    async_validate_jwt_svid,
    async_verify_request,
    async_x509_available,
)
from .const import (
    CALL_TIMEOUT,
    CONF_SOCKET,
    DOMAIN,
    HEALTH_INTERVAL,
    ISSUE_NO_IDENTITY,
    ISSUE_PERMISSION_DENIED,
    ISSUE_UNREACHABLE,
)
from .coordinator import SpiffeConfigEntry, SpiffeCoordinator, async_raise_no_identity
from .errors import SpiffeAuthError, SpiffeError, SpiffeNotReady
from .event_webhook import async_register_event_webhooks
from .services import async_setup_services
from .source import (
    InvalidSocketError,
    SocketMissingError,
    SourceUnavailableError,
    WorkloadApiSource,
)
from .whoami import WhoamiView

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

__all__ = [
    "SpiffeAuthError",
    "SpiffeError",
    "SpiffeNotReady",
    "async_fetch_jwt_svid",
    "async_get_jwt_bundles",
    "async_register_verified_webhook",
    "async_validate_jwt_svid",
    "async_verify_request",
    "async_x509_available",
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the action and the (opt-in) whoami endpoint, once."""
    async_setup_services(hass)
    hass.http.register_view(WhoamiView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SpiffeConfigEntry) -> bool:
    """Connect to the Workload API and start the health checks."""
    uri = entry.data[CONF_SOCKET]
    try:
        source = await hass.async_add_executor_job(WorkloadApiSource, uri, CALL_TIMEOUT)
    except InvalidSocketError as err:
        raise ConfigEntryError(f"Invalid Workload API address {uri}: {err}") from err
    except (SocketMissingError, SourceUnavailableError) as err:
        raise ConfigEntryNotReady(f"The SPIFFE Workload API is not ready: {err}") from err

    try:
        # The agent may still be starting, or a stopped agent may have left its socket file
        # behind. Either way the first fetch fails, and Home Assistant retries the setup.
        bundles = await hass.async_add_executor_job(source.fetch_jwt_bundles)
        x509 = await hass.async_add_executor_job(source.x509_available)
    except SourceUnavailableError as err:
        await hass.async_add_executor_job(source.close)
        if err.permission_denied:
            async_raise_no_identity(hass, str(err))
        raise ConfigEntryNotReady(f"The SPIFFE Workload API is not ready: {err}") from err
    except BaseException:
        await hass.async_add_executor_job(source.close)
        raise

    coordinator = SpiffeCoordinator(hass, entry, source, bundles, x509)
    entry.runtime_data = coordinator

    # Scheduled here rather than by the coordinator, so the checks (which also refresh the bundles
    # the helpers validate with) keep running when entities are disabled or polling is turned off.
    async def _async_check(_now: object) -> None:
        await coordinator.async_refresh()

    entry.async_on_unload(async_track_time_interval(hass, _async_check, HEALTH_INTERVAL))
    ir.async_delete_issue(hass, DOMAIN, ISSUE_UNREACHABLE)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_NO_IDENTITY)

    async_register_event_webhooks(hass, entry)
    # The options are read when a request comes in, so only a change to the webhooks needs a
    # reload.
    subentries = _subentry_snapshot(entry)

    async def _async_entry_updated(hass: HomeAssistant, entry: SpiffeConfigEntry) -> None:
        if _subentry_snapshot(entry) != subentries:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Unload is not called for a setup that failed, so close the connection here.
        await coordinator.async_shutdown()
        await hass.async_add_executor_job(source.close)
        raise
    return True


def _subentry_snapshot(entry: SpiffeConfigEntry) -> dict[str, tuple[str, str, str]]:
    return {
        subentry_id: (subentry.subentry_type, subentry.title, repr(dict(subentry.data)))
        for subentry_id, subentry in entry.subentries.items()
    }


async def async_unload_entry(hass: HomeAssistant, entry: SpiffeConfigEntry) -> bool:
    """Stop the checks and close the connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        await coordinator.async_shutdown()
        await hass.async_add_executor_job(coordinator.source.close)
        _delete_issues(hass)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SpiffeConfigEntry) -> None:
    """Remove the repair issues the entry may have left."""
    _delete_issues(hass)


def _delete_issues(hass: HomeAssistant) -> None:
    for issue in (ISSUE_UNREACHABLE, ISSUE_PERMISSION_DENIED, ISSUE_NO_IDENTITY):
        ir.async_delete_issue(hass, DOMAIN, issue)
