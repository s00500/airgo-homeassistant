"""Switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirGoConfigEntry
from .entity import AirGoSettingEntity
from .protocol import Param


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirGoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    async_add_entities([AirGoScreenOff(entry.runtime_data)])


class AirGoScreenOff(AirGoSettingEntity, SwitchEntity):
    """Turn the display off after 10 minutes of standby."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "screen_off")

    @property
    def is_on(self) -> bool | None:
        """Return the setting."""
        value = self.coordinator.data.get(Param.SCREEN_OFF)
        return None if value is None else value == 1

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        await self.coordinator.async_set_params({Param.SCREEN_OFF: 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        await self.coordinator.async_set_params({Param.SCREEN_OFF: 0})
