"""Binary sensors for the SPIFFE integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
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
            SpiffeReachableSensor(coordinator, "workload_api"),
            SpiffeX509Sensor(coordinator, "x509_available"),
        ]
    )


class SpiffeReachableSensor(SpiffeEntity, BinarySensorEntity):
    """On while the Workload API answers the scheduled check."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.reachable


class SpiffeX509Sensor(SpiffeEntity, BinarySensorEntity):
    """Whether the source offered X.509-SVIDs at setup."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.coordinator.x509_available
