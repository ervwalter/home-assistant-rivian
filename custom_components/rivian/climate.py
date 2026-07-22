"""Support for Rivian climate entities."""

from __future__ import annotations

import logging
from typing import Any, Final

from rivian import VehicleCommand

from homeassistant.components.climate import (
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_COORDINATOR, ATTR_VEHICLE, DOMAIN
from .coordinator import VehicleCoordinator
from .entity import RivianVehicleControlEntity, RivianVehicleEntity
from .r2 import is_r2_vehicle, supports_vehicle_control

_LOGGER = logging.getLogger(__name__)

CLIMATE: Final[ClimateEntityDescription] = ClimateEntityDescription(
    key="cabin_climate", name="Cabin Climate"
)

DEFROST_DEFOG = "Defrost/Defog"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the climate entity."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    vehicles: dict[str, dict[str, Any]] = data[ATTR_VEHICLE]
    coordinators: dict[str, VehicleCoordinator] = data[ATTR_COORDINATOR][ATTR_VEHICLE]

    entities: list[ClimateEntity] = []
    for vehicle_id, vehicle in vehicles.items():
        if is_r2_vehicle(vehicle):
            if "PX_STATE_ALL" in vehicle.get("supported_features", []):
                entities.append(
                    R2ReadOnlyClimateEntity(
                        coordinators[vehicle_id], entry, CLIMATE, vehicle
                    )
                )
        elif supports_vehicle_control(vehicle):
            entities.append(
                RivianClimateEntity(coordinators[vehicle_id], entry, CLIMATE, vehicle)
            )
    async_add_entities(entities)


class R2ReadOnlyClimateEntity(RivianVehicleEntity, ClimateEntity):
    """Read-only R2 cabin temperature and remote-preconditioning state."""

    _attr_max_temp = 29
    _attr_min_temp = 16
    _attr_precision = PRECISION_TENTHS
    _attr_supported_features = ClimateEntityFeature(0)
    _attr_target_temperature_step = 0.5
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    @property
    def available(self) -> bool:
        """Return whether all state needed by the read-only entity is known."""
        return (
            super().available
            and self.current_temperature is not None
            and self.target_temperature is not None
            and self.hvac_mode is not None
        )

    @property
    def current_temperature(self) -> float | None:
        """Return the observed cabin temperature."""
        return self._get_value("cabinClimateInteriorTemperature")

    @property
    def target_temperature(self) -> float | None:
        """Return the observed cabin target temperature."""
        return self._get_value("cabinClimateDriverTemperature")

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Represent remote preconditioning without implying normal HVAC state."""
        return {
            "active": HVACMode.HEAT_COOL,
            "inactive": HVACMode.OFF,
        }.get(self._get_value("cabinPreconditioningStatus"))

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Expose only the observed mode so the entity remains read-only."""
        return [mode] if (mode := self.hvac_mode) is not None else []

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Reject control attempts until the R2 command protocol is supported."""
        raise HomeAssistantError("R2 cabin climate control is not supported")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Reject control attempts until the R2 command protocol is supported."""
        raise HomeAssistantError("R2 cabin climate control is not supported")


class RivianClimateEntity(RivianVehicleControlEntity, ClimateEntity):
    """Representation of a Rivian climate entity."""

    _attr_hvac_mode = HVACMode.OFF
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL, HVACMode.HEAT]
    _attr_max_temp = 29
    _attr_min_temp = 16
    _attr_precision = PRECISION_WHOLE
    _attr_preset_modes = ["LO", "HI", DEFROST_DEFOG]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_target_temperature_step = PRECISION_WHOLE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _enable_turn_on_off_backwards_compatibility = False

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._get_value("cabinClimateInteriorTemperature")

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._get_value("cabinClimateDriverTemperature")

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation mode."""
        if self.preset_mode == DEFROST_DEFOG:
            return HVACMode.HEAT
        if self._get_value("cabinPreconditioningType") != "NONE":
            return HVACMode.HEAT_COOL
        return HVACMode.OFF

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        if self._get_value("defrostDefogStatus") != "Off":
            return DEFROST_DEFOG
        return {0: "LO", 63.5: "HI"}.get(self.target_temperature)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        if hvac_mode == HVACMode.HEAT:
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.CABIN_HVAC_DEFROST_DEFOG, params={"level": 1}
            )
            return
        await self.coordinator.send_vehicle_command(
            command=VehicleCommand.VEHICLE_CABIN_PRECONDITION_DISABLE
            if hvac_mode == HVACMode.OFF
            else VehicleCommand.VEHICLE_CABIN_PRECONDITION_ENABLE
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode == DEFROST_DEFOG:
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.CABIN_HVAC_DEFROST_DEFOG, params={"level": 1}
            )
            return
        await self.async_set_temperature(
            temperature={"LO": 0, "HI": 63.5}.get(preset_mode)
        )

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if self.preset_mode == DEFROST_DEFOG:
            # need to turn off defrost/defog before we can adjust
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.CABIN_HVAC_DEFROST_DEFOG, params={"level": 0}
            )
        if self.hvac_mode == HVACMode.OFF:
            # must turn on preconditioning to adjust temperature
            await self.coordinator.send_vehicle_command(
                command=VehicleCommand.VEHICLE_CABIN_PRECONDITION_ENABLE
            )
        await self.coordinator.send_vehicle_command(
            command=VehicleCommand.CABIN_PRECONDITIONING_SET_TEMP,
            params={"HVAC_set_temp": temperature},
        )
