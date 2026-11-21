"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AirGoCoordinator


class AirGoEntity(CoordinatorEntity[AirGoCoordinator]):
    """An entity of one gun."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AirGoCoordinator, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer="Miniware",
            model="AirGo 140",
            name=coordinator.config_entry.title,
        )

    @property
    def available(self) -> bool:
        """Return whether the gun is connected."""
        return self.coordinator.connected


class AirGoSettingEntity(AirGoEntity):
    """A stored setting: the gun only accepts these while idle."""

    @property
    def available(self) -> bool:
        """Return whether the setting can be changed now."""
        return super().available and not self.coordinator.heating
