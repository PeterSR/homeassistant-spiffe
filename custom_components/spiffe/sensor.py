"""Sensors for the SPIFFE integration."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SpiffeConfigEntry
from .entity import SpiffeEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SpiffeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            SpiffeBundleKeysSensor(coordinator, "bundle_keys"),
            SpiffeLastFetchSensor(coordinator, "last_svid_fetch"),
        ]
    )


class SpiffeBundleKeysSensor(SpiffeEntity, SensorEntity):
    """How many JWT signing keys the trust bundles hold."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.bundle_keys

    @property
    def extra_state_attributes(self) -> dict[str, list[str]]:
        return {"trust_domains": list(self.coordinator.data.trust_domains)}


class SpiffeLastFetchSensor(SpiffeEntity, SensorEntity):
    """When a JWT-SVID was last fetched. Updated at most once per check, disabled by default."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.last_svid_fetch
