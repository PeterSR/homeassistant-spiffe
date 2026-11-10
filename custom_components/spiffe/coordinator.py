"""Health checks for the SPIFFE source, and the trust bundles they keep fresh."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from spiffe import JwtBundleSet

from .const import (
    CONF_SOCKET,
    DOMAIN,
    ISSUE_NO_IDENTITY,
    ISSUE_PERMISSION_DENIED,
    ISSUE_UNREACHABLE,
    UNREACHABLE_GRACE,
)
from .source import SourceUnavailableError, SpiffeSource, bundle_key_count

_LOGGER = logging.getLogger(__name__)

type SpiffeConfigEntry = ConfigEntry[SpiffeCoordinator]


@dataclass(frozen=True, slots=True)
class SpiffeHealth:
    """The outcome of the latest check."""

    reachable: bool
    bundle_keys: int | None
    trust_domains: tuple[str, ...]
    last_svid_fetch: datetime | None


class SpiffeCoordinator(DataUpdateCoordinator[SpiffeHealth]):
    """Checks the source and holds what the helpers need.

    The check is a one-shot bundle fetch. It never raises, so the entities stay available and
    report the source as unreachable instead of going unavailable themselves.

    The coordinator has no update interval of its own: `async_setup_entry` schedules the check,
    because the coordinator's own schedule stops when a user turns off polling for the entry, and
    the bundles must keep being refreshed regardless.
    """

    config_entry: SpiffeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SpiffeConfigEntry,
        source: SpiffeSource,
        bundles: JwtBundleSet,
        x509_available: bool,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.source = source
        self.bundles = bundles
        self.bundles_fetched_at = dt_util.utcnow()
        self.x509_available = x509_available
        self.last_svid_fetch: datetime | None = None
        self._failing_since: datetime | None = None
        # Set on unload, so a check still in flight cannot recreate a repair issue afterwards.
        self.closed = False
        self.async_set_updated_data(self._health(reachable=True))

    def _health(self, *, reachable: bool) -> SpiffeHealth:
        return SpiffeHealth(
            reachable=reachable,
            bundle_keys=bundle_key_count(self.bundles),
            trust_domains=tuple(sorted(b.trust_domain.name for b in self.bundles.bundles)),
            last_svid_fetch=self.last_svid_fetch,
        )

    async def async_shutdown(self) -> None:
        self.closed = True
        await super().async_shutdown()

    async def _async_update_data(self) -> SpiffeHealth:
        try:
            bundles = await self.hass.async_add_executor_job(self.source.fetch_jwt_bundles)
        except SourceUnavailableError as err:
            if self.closed:
                return self._health(reachable=False)
            if err.permission_denied:
                # It answers, but has no identity for us: a different problem from not answering.
                self.note_no_identity(str(err))
                self._failing_since = None
                ir.async_delete_issue(self.hass, DOMAIN, ISSUE_UNREACHABLE)
            else:
                self._note_failure(str(err))
            return self._health(reachable=False)
        self.bundles = bundles
        self.bundles_fetched_at = dt_util.utcnow()
        self._note_success()
        return self._health(reachable=True)

    def _note_failure(self, error: str) -> None:
        now = dt_util.utcnow()
        if self._failing_since is None:
            self._failing_since = now
            _LOGGER.warning("The SPIFFE Workload API is not answering: %s", error)
        if now - self._failing_since >= UNREACHABLE_GRACE:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_UNREACHABLE,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_UNREACHABLE,
                translation_placeholders={
                    "socket": self.config_entry.data[CONF_SOCKET],
                    "error": error,
                },
            )

    def _note_success(self) -> None:
        if self._failing_since is not None:
            _LOGGER.info("The SPIFFE Workload API is answering again")
        self._failing_since = None
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_UNREACHABLE)
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_NO_IDENTITY)

    def note_no_identity(self, error: str) -> None:
        """Raise a repair issue: the source refuses even the bundles to Home Assistant."""
        async_raise_no_identity(self.hass, error)

    def note_svid_fetched(self) -> None:
        """Record a successful JWT-SVID fetch.

        The entities only pick this up on the next scheduled check, which keeps the recorder
        from writing a state for every token fetched.
        """
        self.last_svid_fetch = dt_util.utcnow()
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_PERMISSION_DENIED)

    def note_permission_denied(self, error: str) -> None:
        """Raise a repair issue: the source answers but refused a JWT-SVID."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_PERMISSION_DENIED,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_PERMISSION_DENIED,
            translation_placeholders={"error": error},
        )


def async_raise_no_identity(hass: HomeAssistant, error: str) -> None:
    """Raise the repair issue for a Workload API that has no identity for Home Assistant."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_NO_IDENTITY,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_NO_IDENTITY,
        translation_placeholders={"error": error},
    )
