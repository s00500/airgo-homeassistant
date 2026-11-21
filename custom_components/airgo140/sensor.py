"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AirGoConfigEntry
from .entity import AirGoEntity
from .protocol import UNAVAILABLE, Param, firmware_string


@dataclass(frozen=True, kw_only=True)
class AirGoSensorDescription(SensorEntityDescription):
    """Describes a sensor."""

    param: int
    convert: Callable[[int], float | int | str] = lambda v: v
    is_temperature: bool = False


SENSORS = (
    AirGoSensorDescription(
        key="temperature",
        param=Param.TEMPERATURE,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        is_temperature=True,
    ),
    AirGoSensorDescription(
        key="target_temperature",
        param=Param.TARGET_TEMP,
        device_class=SensorDeviceClass.TEMPERATURE,
        is_temperature=True,
    ),
    AirGoSensorDescription(
        key="output",
        param=Param.OUTPUT_PERCENT,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    AirGoSensorDescription(
        key="active_gear",
        param=Param.ACTIVE_GEAR,
    ),
    AirGoSensorDescription(
        key="input_voltage",
        param=Param.INPUT_MV,
        convert=lambda v: round(v / 1000, 2),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    AirGoSensorDescription(
        key="pd_power",
        param=Param.PD_POWER,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AirGoSensorDescription(
        key="heater_resistance",
        param=Param.HEATER_MOHM,
        convert=lambda v: round(v / 1000, 2),
        native_unit_of_measurement="Ω",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AirGoSensorDescription(
        key="firmware",
        param=Param.FIRMWARE,
        convert=firmware_string,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AirGoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    async_add_entities(AirGoSensor(entry.runtime_data, d) for d in SENSORS)


class AirGoSensor(AirGoEntity, SensorEntity):
    """A sensor backed by one parameter."""

    entity_description: AirGoSensorDescription

    def __init__(self, coordinator, description: AirGoSensorDescription) -> None:
        """Initialize."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        """Return the value; the gun reports -5000 for not applicable."""
        value = self.coordinator.data.get(self.entity_description.param)
        if value is None or value == UNAVAILABLE:
            return None
        return self.entity_description.convert(value)

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, following the gun's unit setting for temperatures."""
        if self.entity_description.is_temperature:
            if self.coordinator.fahrenheit:
                return UnitOfTemperature.FAHRENHEIT
            return UnitOfTemperature.CELSIUS
        return self.entity_description.native_unit_of_measurement
