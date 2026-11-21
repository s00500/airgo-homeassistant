"""Selects: heat control and enumerated settings."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirGoConfigEntry
from .entity import AirGoEntity, AirGoSettingEntity
from .protocol import Param

OFF = "off"
GEARS = ["gear1", "gear2", "gear3", "gear4", "gear5"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirGoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up selects."""
    c = entry.runtime_data
    async_add_entities(
        [
            AirGoHeatSelect(c),
            AirGoSettingSelect(c, "temp_unit", Param.TEMP_UNIT, ["celsius", "fahrenheit"]),
            AirGoSettingSelect(c, "cool_mode", Param.FAST_COOL, ["default", "fast"]),
            AirGoSettingSelect(c, "language", Param.LANGUAGE, ["en", "cn", "de", "ru"]),
        ]
    )


class AirGoHeatSelect(AirGoEntity, SelectEntity):
    """Starts, switches and stops heating.

    Disabled by default: this remotely turns on a heater of up to 450 °C, so an
    automation or a stray tap must not be able to do that until the user has
    deliberately enabled the entity.
    """

    _attr_entity_registry_enabled_default = False
    _attr_options = [OFF, *GEARS]

    def __init__(self, coordinator) -> None:
        """Initialize."""
        super().__init__(coordinator, "heat")

    @property
    def current_option(self) -> str:
        """Return the active gear."""
        gear = self.coordinator.data.get(Param.ACTIVE_GEAR, 0)
        return GEARS[gear - 1] if 1 <= gear <= 5 else OFF

    async def async_select_option(self, option: str) -> None:
        """Start, switch or stop."""
        if option == OFF:
            await self.coordinator.async_stop_heating()
        else:
            await self.coordinator.async_start_gear(GEARS.index(option) + 1)


class AirGoSettingSelect(AirGoSettingEntity, SelectEntity):
    """A stored enumerated setting."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key: str, pid: int, options: list[str]) -> None:
        """Initialize."""
        super().__init__(coordinator, key)
        self._pid = pid
        self._attr_options = options

    @property
    def current_option(self) -> str | None:
        """Return the current option."""
        value = self.coordinator.data.get(self._pid)
        if value is None or not 0 <= value < len(self.options):
            return None
        return self.options[value]

    async def async_select_option(self, option: str) -> None:
        """Write the setting."""
        await self.coordinator.async_set_params({self._pid: self.options.index(option)})
