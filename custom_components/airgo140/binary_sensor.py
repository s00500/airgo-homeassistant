"""Binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirGoConfigEntry
from .entity import AirGoEntity
from .protocol import FLAG_HEATER_FAULT, FLAG_LOW_POWER, Param


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirGoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            AirGoHeating(coordinator),
            AirGoFlag(coordinator, "low_power", FLAG_LOW_POWER),
            AirGoFlag(coordinator, "heater_fault", FLAG_HEATER_FAULT),
        ]
    )


class AirGoHeating(AirGoEntity, BinarySensorEntity):
    """Whether the gun is heating."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "heating")

    @property
    def is_on(self) -> bool:
        """Return true while heating."""
        return self.coordinator.heating


class AirGoFlag(AirGoEntity, BinarySensorEntity):
    """A problem bit of the status flags."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, key: str, mask: int) -> None:
        """Initialize."""
        super().__init__(coordinator, key)
        self._mask = mask

    @property
    def is_on(self) -> bool:
        """Return true when the bit is set."""
        return bool(self.coordinator.data.get(Param.FLAGS, 0) & self._mask)
