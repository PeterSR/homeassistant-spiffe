"""Give Home Assistant a SPIFFE identity that other integrations can use.

The integration talks to a SPIFFE Workload API (any implementation: a SPIRE agent, spiffex, tbot,
or anything else that serves one) and offers other custom integrations a few helpers:

    from custom_components.spiffe import async_fetch_jwt_svid

    token = await async_fetch_jwt_svid(hass, "https://api.example.org")
    headers = {"Authorization": f"Bearer {token}"}

Add "spiffe" to "dependencies" in your manifest.json so it is set up before you. Tokens are short
lived: fetch one for each request, or at least each time you need one, instead of storing it.
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from spiffe import JwtBundleSet

from .const import (
    CALL_TIMEOUT,
    CONF_SOCKET,
    DOMAIN,
    HEALTH_INTERVAL,
    ISSUE_PERMISSION_DENIED,
    ISSUE_UNREACHABLE,
    MAX_BUNDLE_AGE,
)
from .coordinator import SpiffeConfigEntry, SpiffeCoordinator
from .source import (
    AudienceNotAvailableError,
    InvalidSocketError,
    SocketMissingError,
    SourceUnavailableError,
    SpiffeSourceError,
    WorkloadApiSource,
    validate_jwt_svid,
)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

__all__ = [
    "SpiffeError",
    "SpiffeNotReady",
    "async_fetch_jwt_svid",
    "async_get_jwt_bundles",
    "async_validate_jwt_svid",
    "async_x509_available",
]


class SpiffeNotReady(HomeAssistantError):
    """The SPIFFE integration is not set up or not loaded.

    A caller that needs an identity to work should raise ConfigEntryNotReady when it gets this, so
    Home Assistant retries its setup later.
    """


class SpiffeError(HomeAssistantError):
    """The SPIFFE source could not provide what was asked, or a token did not validate."""


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

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Unload is not called for a setup that failed, so close the connection here.
        await coordinator.async_shutdown()
        await hass.async_add_executor_job(source.close)
        raise
    return True


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
    for issue in (ISSUE_UNREACHABLE, ISSUE_PERMISSION_DENIED):
        ir.async_delete_issue(hass, DOMAIN, issue)


def _coordinator(hass: HomeAssistant) -> SpiffeCoordinator:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise SpiffeNotReady("The SPIFFE integration is not set up or not loaded")
    return entries[0].runtime_data


async def async_fetch_jwt_svid(
    hass: HomeAssistant,
    audience: str | Iterable[str],
    subject: str | None = None,
) -> str:
    """Return a JWT-SVID for this audience (or audiences), to send as a bearer token.

    `subject` picks one SPIFFE ID when the source holds several; leave it out otherwise.
    Raises SpiffeNotReady when the integration is not loaded, SpiffeError when the source
    cannot provide the token, and ValueError for an empty audience or invalid subject.
    The token itself is never logged or stored by this integration.
    """
    coordinator = _coordinator(hass)
    try:
        svid = await hass.async_add_executor_job(
            coordinator.source.fetch_jwt_svid, audience, subject
        )
    except SourceUnavailableError as err:
        if err.permission_denied:
            coordinator.note_permission_denied(str(err))
        raise SpiffeError(f"Could not get a JWT-SVID: {err}") from err
    except AudienceNotAvailableError as err:
        raise SpiffeError(f"Could not get a JWT-SVID: {err}") from err
    coordinator.note_svid_fetched()
    return svid.token


async def async_get_jwt_bundles(hass: HomeAssistant) -> JwtBundleSet:
    """Return a copy of the JWT bundles from the latest successful check.

    They are refreshed every minute while the Workload API answers. Raises SpiffeError when they
    could not be refreshed for a while, as validation does.
    """
    coordinator = _coordinator(hass)
    _check_bundle_age(coordinator)
    bundles = coordinator.bundles
    return JwtBundleSet({b.trust_domain: b for b in bundles.bundles})


def _check_bundle_age(coordinator: SpiffeCoordinator) -> None:
    age = dt_util.utcnow() - coordinator.bundles_fetched_at
    if age > MAX_BUNDLE_AGE:
        raise SpiffeError(
            f"The trust bundles have not been refreshed for {int(age.total_seconds() // 60)}"
            " minutes; not using them"
        )


async def async_validate_jwt_svid(hass: HomeAssistant, token: str, audience: str) -> str:
    """Validate a JWT-SVID someone presented to Home Assistant, and return its SPIFFE ID.

    It must be for `audience`, unexpired, and signed by a key in the cached bundles. Check the
    returned SPIFFE ID against the caller you expect: a valid token only says who sent it.
    Raises SpiffeNotReady, SpiffeError (also when the bundles could not be refreshed for a
    while, so a key the issuer withdrew is not trusted indefinitely), or ValueError for an empty
    or non-string audience.
    """
    coordinator = _coordinator(hass)
    if not isinstance(audience, str) or not audience.strip():
        raise ValueError("the audience must be a non-empty string")
    _check_bundle_age(coordinator)
    try:
        svid = await hass.async_add_executor_job(
            validate_jwt_svid, coordinator.bundles, token, audience
        )
    except SpiffeSourceError as err:
        raise SpiffeError(str(err)) from err
    return svid.spiffe_id


async def async_x509_available(hass: HomeAssistant) -> bool:
    """Return whether the source offered X.509-SVIDs when the integration was set up."""
    return _coordinator(hass).x509_available
