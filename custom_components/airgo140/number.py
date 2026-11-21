"""Numbers: live target and fan while heating, stored settings while idle."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirGoConfigEntry
from .entity import AirGoEntity, AirGoSettingEntity
from .protocol import UNAVAILABLE, Param, gear_fan_param, gear_temp_param, value_range


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirGoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up numbers."""
    c = entry.runtime_data
    entities: list[NumberEntity] = [
        AirGoLiveTemperature(c, "live_target_temperature", Param.TARGET_TEMP),
        AirGoLiveNumber(c, "live_fan_level", Param.FAN_LEVEL),
    ]
    for gear in range(1, 6):
        entities.append(AirGoGearTemperature(c, f"gear{gear}_temperature", gear_temp_param(gear)))
        entities.append(AirGoSettingNumber(c, f"gear{gear}_fan", gear_fan_param(gear)))
    entities += [
        AirGoSettingNumber(c, "idle_time", Param.IDLE_TIME, unit=UnitOfTime.MINUTES),
        AirGoSettingNumber(c, "idle_detect", Param.IDLE_DETECT, unit=UnitOfTime.MINUTES),
        AirGoSettingNumber(c, "pd_limit", Param.PD_LIMIT, unit=UnitOfPower.WATT),
        AirGoSettingNumber(c, "dc_limit", Param.DC_LIMIT, unit=UnitOfPower.WATT),
        AirGoSettingNumber(c, "backlight", Param.BACKLIGHT),
        AirGoSettingNumber(c, "volume", Param.VOLUME),
    ]
    async_add_entities(entities)


class _ParamNumber(NumberEntity):
    """Shared parameter behavior."""

    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _pid: int

    @property
    def native_value(self) -> float | None:
        """Return the value."""
        value = self.coordinator.data.get(self._pid)
        return None if value is None or value == UNAVAILABLE else value

    @property
    def native_min_value(self) -> float:
        """Return the lower limit."""
        return value_range(self._pid, self.coordinator.fahrenheit)[0]

    @property
    def native_max_value(self) -> float:
        """Return the upper limit."""
        return value_range(self._pid, self.coordinator.fahrenheit)[1]

    async def async_set_native_value(self, value: float) -> None:
        """Write the value."""
        await self.coordinator.async_set_params({self._pid: int(value)})


class _TemperatureUnit:
    """Follows the gun's unit setting."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE

    @property
    def native_unit_of_measurement(self) -> str:
        """Return the unit."""
        if self.coordinator.fahrenheit:
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS


class AirGoLiveNumber(_ParamNumber, AirGoEntity):
    """Accepted by the gun only while heating."""

    def __init__(self, coordinator, key: str, pid: int) -> None:
        """Initialize."""
        AirGoEntity.__init__(self, coordinator, key)
        self._pid = pid

    @property
    def available(self) -> bool:
        """Return whether the gun is heating."""
        return AirGoEntity.available.fget(self) and self.coordinator.heating


class AirGoLiveTemperature(_TemperatureUnit, AirGoLiveNumber):
    """Live target temperature."""


class AirGoSettingNumber(_ParamNumber, AirGoSettingEntity):
    """A stored numeric setting."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, key: str, pid: int, unit: str | None = None) -> None:
        """Initialize."""
        AirGoSettingEntity.__init__(self, coordinator, key)
        self._pid = pid
        self._attr_native_unit_of_measurement = unit


class AirGoGearTemperature(_TemperatureUnit, AirGoSettingNumber):
    """Stored temperature of a gear."""
