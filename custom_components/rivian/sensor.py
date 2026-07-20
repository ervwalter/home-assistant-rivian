"""Rivian (Unofficial)"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
import logging
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_UNAVAILABLE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, ATTR_WALLBOX, DOMAIN, SENSORS
from .coordinator import DriverKeyCoordinator, VehicleCoordinator, WallboxCoordinator
from .data_classes import (
    RivianSensorEntityDescription,
    RivianWallboxSensorEntityDescription,
)
from .entity import (
    RivianChargingEntity,
    RivianEntity,
    RivianVehicleEntity,
    RivianWallboxEntity,
)
from .r2 import R2_PX_SENSOR_KEYS, R2_SENSOR_KEYS, is_r2_vehicle
from .r2_coordinator import R2ChargingCoordinator

_LOGGER = logging.getLogger(__name__)

RIVIAN_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"


def vehicle_sensor_descriptions(
    vehicle: dict[str, Any],
) -> tuple[RivianSensorEntityDescription, ...]:
    """Return the stable sensor profile selected from model and capabilities."""
    if is_r2_vehicle(vehicle):
        descriptions = tuple(
            description
            for description in SENSORS["R1"]
            if description.key in R2_SENSOR_KEYS
        )
        if "PX_STATE_ALL" in vehicle.get("supported_features", []):
            descriptions += (
                tuple(
                    replace(
                        description,
                        options=[*(description.options or ()), "Unknown"],
                    )
                    if description.key == "power_state"
                    else description
                    for description in SENSORS["R1"]
                    if description.key in R2_PX_SENSOR_KEYS
                )
                + R2_VEHICLE_SENSORS
            )
        return descriptions
    return tuple(
        description
        for model, model_descriptions in SENSORS.items()
        if model in vehicle["model"]
        for description in model_descriptions
    )


def charging_sensor_descriptions(
    vehicle: dict[str, Any],
) -> tuple[RivianSensorEntityDescription, ...]:
    """Return legacy charging sensors or the capability-gated R2 profile."""
    if not is_r2_vehicle(vehicle):
        return CHARGING_SENSORS
    if "CHARG_DATA_PX" in vehicle.get("supported_features", []):
        return R2_CHARGING_SENSORS
    return ()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, Any] = data[ATTR_COORDINATOR]

    # Add vehicle entities
    vehicle_coordinators: dict[str, VehicleCoordinator] = coordinators[ATTR_VEHICLE]
    entities: list[RivianEntity] = []
    for vehicle_id, vehicle in vehicles.items():
        entities.extend(
            RivianSensorEntity(
                vehicle_coordinators[vehicle_id], entry, description, vehicle
            )
            for description in vehicle_sensor_descriptions(vehicle)
        )

    # Add charging entities
    for vehicle_id, vehicle in vehicles.items():
        entities.extend(
            RivianChargingSensorEntity(
                vehicle_coordinators[vehicle_id].charging_coordinator,
                description,
                vehicle["vin"],
            )
            for description in charging_sensor_descriptions(vehicle)
        )

    # Add drivers and keys entities
    entities.extend(
        RivianDriverSensorEntity(
            vehicle_coordinators[vehicle_id].drivers_coordinator,
            description,
            vehicle["vin"],
        )
        for vehicle_id, vehicle in vehicles.items()
        for description in DRIVER_SENSORS
    )

    # Add wallbox entities
    wallbox_coordinator: WallboxCoordinator = coordinators[ATTR_WALLBOX]
    entities.extend(
        RivianWallboxSensorEntity(wallbox_coordinator, description, wallbox)
        for wallbox in wallbox_coordinator.data
        for description in WALLBOX_SENSORS
    )

    async_add_entities(entities)


class RivianSensorEntity(RivianVehicleEntity, SensorEntity):
    """Representation of a Rivian sensor entity."""

    entity_description: RivianSensorEntityDescription

    @property
    def native_value(self) -> str | None:
        """Return the value reported by the sensor."""
        if _fn := self.entity_description.value_fn:
            return _fn(self.coordinator)

        if (val := self._get_value(self.entity_description.field)) is None:
            return STATE_UNAVAILABLE if not self.native_unit_of_measurement else None

        rval = _fn(val) if (_fn := self.entity_description.value_lambda) else val
        options = list(self.options or ())
        if self.device_class == SensorDeviceClass.ENUM and rval not in options:
            _LOGGER.error(
                "Sensor %s provides state value '%s', which is not in the list of known options. Please consider opening an issue at https://github.com/bretterer/home-assistant-rivian/issues with the following info: 'field: \"%s\" / value: \"%s\"'",
                self.name,
                rval,
                self.entity_description.field,
                val,
            )
            self._attr_options = [*options, str(rval)]
        return rval

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        try:
            entity = self.coordinator.data[self.entity_description.field]
            if entity is None:
                return None
            if self.entity_description.value_lambda is None:
                return {
                    "last_update": entity["timeStamp"],
                }
            return {
                "native_value": entity["value"],
                "last_update": entity["timeStamp"],
                "history": str(entity["history"]),
            }
        except KeyError:
            return None


class RivianChargingSensorEntity(RivianChargingEntity, SensorEntity):
    """Representation of a Rivian charging sensor entity."""

    entity_description: RivianSensorEntityDescription

    @property
    def available(self) -> bool:
        """Return whether this R2 field has a current direct observation."""
        if isinstance(self.coordinator, R2ChargingCoordinator):
            return (
                super().available
                and self.entity_description.field in self.coordinator.data
                and self.coordinator.data[self.entity_description.field] is not None
            )
        return super().available

    @property
    def native_value(self) -> str | float | None:
        """Return the value reported by the sensor."""
        val = self.coordinator.data.get(self.entity_description.field)
        if isinstance(val, dict):
            val = val["value"]
        if value_fn := self.entity_description.value_lambda:
            return value_fn(val)
        return val

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit of measurement of the sensor, if any."""
        if self.entity_description.field == "currentPrice":
            return self.coordinator.data.get(
                "currentCurrency", self.hass.config.currency
            )
        return super().native_unit_of_measurement


CHARGING_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="charging_cost",
        field="currentPrice",
        name="Charging Cost",
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_delivered",
        field="totalChargedEnergy",
        name="Charging Energy Delivered",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_range_added",
        field="rangeAddedThisSession",
        name="Charging Range Added",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_unit_of_measurement=UnitOfLength.MILES,
    ),
    RivianSensorEntityDescription(
        key="charging_rate",
        field="kilometersChargedPerHour",
        name="Charging Rate",
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
    ),
    RivianSensorEntityDescription(
        key="charging_speed",
        field="power",
        name="Charging Speed",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianSensorEntityDescription(
        key="charging_start_time",
        field="startTime",
        name="Charging Start Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_lambda=lambda val: (
            datetime.strptime(val, RIVIAN_TIMESTAMP_FORMAT) if val else val
        ),
    ),
    RivianSensorEntityDescription(
        key="charging_time_elapsed",
        field="timeElapsed",
        name="Charging Time Elapsed",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)

R2_VEHICLE_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="drive_mode",
        field="driveMode",
        name="Drive Mode",
        icon="mdi:car-speed-limiter",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "All-Purpose",
            "Sport",
            "Conserve",
            "Snow",
            "All-Terrain",
            "Soft Sand",
            "Rally",
            "unknown",
        ],
    ),
    RivianSensorEntityDescription(
        key="gear_status",
        field="gearStatus",
        name="Gear Selector",
        icon="mdi:car-shift-pattern",
        device_class=SensorDeviceClass.ENUM,
        options=["Drive", "Neutral", "Park", "Reverse", "unknown"],
    ),
    RivianSensorEntityDescription(
        key="battery_energy",
        field="batteryEnergy",
        name="Battery Energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="battery_cell_average_temperature",
        field="batteryCellAverageTemperature",
        name="Battery Cell Average Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="battery_cell_max_temperature",
        field="batteryCellMaxTemperature",
        name="Battery Cell Maximum Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="battery_cell_min_temperature",
        field="batteryCellMinTemperature",
        name="Battery Cell Minimum Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
)

R2_CHARGING_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = tuple(
    replace(description, name="Charging Power")
    if description.key == "charging_speed"
    else description
    for description in CHARGING_SENSORS
    if description.key
    in {
        "charging_energy_delivered",
        "charging_range_added",
        "charging_rate",
        "charging_speed",
        "charging_start_time",
        "charging_time_elapsed",
    }
) + (
    RivianSensorEntityDescription(
        key="charging_session_time_remaining",
        field="timeRemaining",
        name="Charging Time Remaining",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_to_pack",
        field="packChargedEnergy",
        name="Charging Energy To Battery",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_thermal",
        field="thermalChargedEnergy",
        name="Charging Energy For Thermal Management",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_outlets",
        field="outletsChargedEnergy",
        name="Charging Energy For Outlets",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_energy_system",
        field="systemChargedEnergy",
        name="Charging Energy For Vehicle Systems",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=1,
    ),
    RivianSensorEntityDescription(
        key="charging_status",
        field="sessionStatus",
        translation_key="r2_charging_status",
        device_class=SensorDeviceClass.ENUM,
        options=["unplugged", "charging", "stopped", "unknown"],
    ),
)


class RivianWallboxSensorEntity(RivianWallboxEntity, SensorEntity):
    """Representation of a Rivian wallbox sensor entity."""

    entity_description: RivianWallboxSensorEntityDescription

    @property
    def native_value(self) -> StateType:
        """Return the value reported by the sensor."""
        value = self.wallbox[self.entity_description.field]
        if self.device_class == SensorDeviceClass.ENUM:
            return value.lower()
        return value


WALLBOX_SENSORS = (
    RivianWallboxSensorEntityDescription(
        key="charging_status",
        field="chargingStatus",
        name="Charging status",
        icon="mdi:ev-plug-type1",
        device_class=SensorDeviceClass.ENUM,
        options=["unavailable", "available", "disconnected", "plugged_in", "charging"],
        translation_key="charging_status",
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage",
        field="currentAmps",
        name="Amperage",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="amperage_maximum",
        field="maxAmps",
        name="Amperage maximum",
        device_class=SensorDeviceClass.CURRENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="power",
        field="power",
        name="Power",
        device_class=SensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    RivianWallboxSensorEntityDescription(
        key="power_maximum",
        field="maxPower",
        name="Power maximum",
        device_class=SensorDeviceClass.POWER,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        suggested_unit_of_measurement=UnitOfPower.KILO_WATT,
    ),
    RivianWallboxSensorEntityDescription(
        key="voltage",
        field="currentVoltage",
        name="Voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    RivianWallboxSensorEntityDescription(
        key="voltage_maximum",
        field="maxVoltage",
        name="Voltage maximum",
        device_class=SensorDeviceClass.VOLTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

DRIVER_SENSORS: Final[tuple[RivianSensorEntityDescription, ...]] = (
    RivianSensorEntityDescription(
        key="drivers",
        icon="mdi:account-multiple",
        name="Drivers",
        field="invitedUsers",
        value_lambda=lambda data: len(
            [user for user in (data or []) if user["__typename"] == "ProvisionedUser"]
        ),
    ),
    RivianSensorEntityDescription(
        key="keys",
        icon="mdi:car-key",
        name="Keys",
        field="invitedUsers",
        value_lambda=lambda data: len(
            [
                keys
                for user in (data or [])
                if user["__typename"] == "ProvisionedUser"
                for keys in user.get("devices", [])
            ]
        ),
    ),
)


class RivianDriverSensorEntity(RivianEntity[DriverKeyCoordinator], SensorEntity):
    """Representation of a Rivian driver sensor entity."""

    entity_description: RivianSensorEntityDescription
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: DriverKeyCoordinator,
        entity_description: RivianSensorEntityDescription,
        vin: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, vin)})
        self._attr_unique_id = f"{vin}-{entity_description.key}"

    @property
    def native_value(self) -> int:
        """Return the value reported by the sensor."""
        if self.coordinator.data:
            data = self.coordinator.data.get(self.entity_description.field)
            return self.entity_description.value_lambda(data)
        return 0

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes."""
        if self.entity_description.key == "keys":

            def get_count(key: str) -> int:
                field = self.entity_description.field
                return len(
                    [
                        keys
                        for user in (self.coordinator.data.get(field) or [])
                        if user["__typename"] == "ProvisionedUser"
                        for keys in user.get("devices", [])
                        if keys[key]
                    ]
                )

            return {"paired": get_count("isPaired"), "enabled": get_count("isEnabled")}
        return super().extra_state_attributes
