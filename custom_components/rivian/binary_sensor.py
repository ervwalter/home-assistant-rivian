"""Rivian (Unofficial)"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, BINARY_SENSORS, DOMAIN
from .coordinator import VehicleCoordinator
from .data_classes import RivianBinarySensorEntityDescription
from .entity import RivianVehicleEntity
from .r2 import is_r2_vehicle


def binary_sensor_descriptions(
    vehicle: dict[str, Any],
) -> tuple[RivianBinarySensorEntityDescription, ...]:
    """Return the stable binary-sensor profile for a vehicle."""
    if is_r2_vehicle(vehicle):
        descriptions = tuple(
            description
            for description in R2_BINARY_SENSORS
            if (
                description.key in {"charger_state", "charger_status"}
                and "CHARG_DATA_PX" in vehicle.get("supported_features", [])
            )
            or (
                description.key not in {"charger_state", "charger_status"}
                and "PX_STATE_ALL" in vehicle.get("supported_features", [])
            )
        )
        if "PX_STATE_ALL" in vehicle.get("supported_features", []):
            descriptions += tuple(
                description
                for description in BINARY_SENSORS["R1"]
                if description.key == "use_state"
            )
        return descriptions
    return tuple(
        description
        for model, model_descriptions in BINARY_SENSORS.items()
        if model in vehicle["model"]
        for description in model_descriptions
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the binary sensor entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, Any] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities: list[RivianBinarySensorEntity] = []
    for vehicle_id, vehicle in vehicles.items():
        entities.extend(
            RivianBinarySensorEntity(
                coordinators[vehicle_id], entry, description, vehicle
            )
            for description in binary_sensor_descriptions(vehicle)
        )

    async_add_entities(entities)


class RivianBinarySensorEntity(RivianVehicleEntity, BinarySensorEntity):
    """Rivian Binary Sensor Entity."""

    entity_description: RivianBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: VehicleCoordinator,
        config_entry: ConfigEntry,
        description: RivianBinarySensorEntityDescription,
        vehicle: dict[str, Any],
    ) -> None:
        """Create a Rivian binary sensor."""
        super().__init__(coordinator, config_entry, description, vehicle)

    @property
    def available(self) -> bool:
        """Return the availability of the entity."""
        fields = self.entity_description.field
        if isinstance(fields, set):
            return self._available and any(
                self._get_value(entity_key) for entity_key in fields
            )
        return super().available

    @property
    def is_on(self) -> bool | None:
        """Return true if sensor is on."""
        fields = self.entity_description.field
        if isinstance(fields, set):
            return self.entity_description.on_value in (
                self._get_value(entity_key) for entity_key in fields
            )
        if (val := self._get_value(fields)) is not None:
            values = self.entity_description.on_value
            on_values = tuple(values) if isinstance(values, list) else (values,)
            result = val in on_values
            return result if not self.entity_description.negate else not result
        return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the device."""
        field = self.entity_description.field
        if isinstance(field, set):
            return None
        try:
            entity = self.coordinator.data[field]
            if entity is None:
                return None
            return {
                "value": entity["value"],
                "last_update": entity["timeStamp"],
                "history": str(entity["history"]),
            }
        except KeyError:
            return None


R2_BINARY_SENSORS = (
    RivianBinarySensorEntityDescription(
        key="cabin_preconditioning_status",
        field="cabinPreconditioningStatus",
        name="Cabin Climate Preconditioning",
        device_class=BinarySensorDeviceClass.RUNNING,
        on_value="active",
    ),
    RivianBinarySensorEntityDescription(
        key="pet_mode_status",
        field="petModeStatus",
        name="Pet Mode",
        icon="mdi:paw",
        on_value="On",
    ),
    RivianBinarySensorEntityDescription(
        key="charger_state",
        field="isCharging",
        name="Charging Status",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    RivianBinarySensorEntityDescription(
        key="charger_status",
        field="isPluggedIn",
        name="Charger Connection",
        device_class=BinarySensorDeviceClass.PLUG,
    ),
    RivianBinarySensorEntityDescription(
        key="door_front_left_closed",
        field="doorFrontLeftClosed",
        name="Door Front Left",
        device_class=BinarySensorDeviceClass.DOOR,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="door_front_right_closed",
        field="doorFrontRightClosed",
        name="Door Front Right",
        device_class=BinarySensorDeviceClass.DOOR,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="door_rear_left_closed",
        field="doorRearLeftClosed",
        name="Door Rear Left",
        device_class=BinarySensorDeviceClass.DOOR,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="door_rear_right_closed",
        field="doorRearRightClosed",
        name="Door Rear Right",
        device_class=BinarySensorDeviceClass.DOOR,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="closure_frunk_closed",
        field="closureFrunkClosed",
        name="Front Trunk",
        device_class=BinarySensorDeviceClass.DOOR,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="closure_liftgate_closed",
        field="closureLiftgateClosed",
        name="Liftgate",
        device_class=BinarySensorDeviceClass.DOOR,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="window_front_left_closed",
        field="windowFrontLeftClosed",
        name="Window Front Left",
        device_class=BinarySensorDeviceClass.WINDOW,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="window_front_right_closed",
        field="windowFrontRightClosed",
        name="Window Front Right",
        device_class=BinarySensorDeviceClass.WINDOW,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="window_rear_left_closed",
        field="windowRearLeftClosed",
        name="Window Rear Left",
        device_class=BinarySensorDeviceClass.WINDOW,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="window_rear_right_closed",
        field="windowRearRightClosed",
        name="Window Rear Right",
        device_class=BinarySensorDeviceClass.WINDOW,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="window_liftgate_closed",
        field="windowLiftgateClosed",
        name="Window Liftgate",
        device_class=BinarySensorDeviceClass.WINDOW,
        on_value="open",
    ),
    RivianBinarySensorEntityDescription(
        key="locked_state",
        field="r2AllLocked",
        name="Locked State",
        device_class=BinarySensorDeviceClass.LOCK,
        on_value=False,
    ),
)
