"""Buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirGoConfigEntry
from .entity import AirGoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirGoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    async_add_entities([AirGoStopButton(entry.runtime_data)])


class AirGoStopButton(AirGoEntity, ButtonEntity):
    """Stops heating. Enabled by default: stopping is always the safe direction."""

    def __init__(self, coordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "stop")

    async def async_press(self) -> None:
        """Stop heating."""
        await self.coordinator.async_stop_heating()
