"""R2 vehicle observation and entity-profile helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

R2_MODEL = "R2"

R2_PARALLAX_RVMS_BY_FEATURE = {
    "ACTIVE_TRIP": {
        "navigation.navigation_service.trip_info",
        "navigation.navigation_service.trip_progress",
    },
    "CHARG_DATA_PX": {
        "charging.session.status",
        "charging.session.time_estimation",
        "energy_edge_compute.graphs.charge_session_breakdown",
        "energy_edge_compute.graphs.charging_graph_global",
    },
    "PX_STATE_ALL": {
        "body.closures.states",
        "body.locks.states",
        "comfort.cabin.cabin_preconditioning_status",
        "comfort.cabin.cabin_temperatures",
        "dynamics.tires.state",
        "dynamics.vehicle.drive_mode",
        "dynamics.vehicle.gear",
        "dynamics.vehicle.gnss",
        "dynamics.vehicle.odometer",
        "dynamics.vehicle.range",
        "energy.high_voltage.battery_state",
        "vehicle.power.state",
    },
    "TRIP_NAV_PX": {
        "navigation.navigation_service.trip_info",
        "navigation.navigation_service.trip_progress",
    },
}

R2_SENSOR_KEYS = {
    "active_driver",
    "battery_capacity",
    "battery_level",
    "battery_limit",
    "distance_to_empty",
    "ota_current_version",
    "service_mode",
    "vehicle_mileage",
}

R2_PX_SENSOR_KEYS = {
    "altitude",
    "bearing",
    "cabin_temperature",
    "power_state",
    "speed",
    "tire_pressure_front_left",
    "tire_pressure_front_right",
    "tire_pressure_rear_left",
    "tire_pressure_rear_right",
}

R2_NAVIGATION_SENSOR_KEYS = {
    "navigation_destination",
    "navigation_distance_remaining",
    "navigation_eta",
    "navigation_time_remaining",
}

R2_CLOSURE_FIELDS = {
    1: "doorFrontLeftClosed",
    2: "doorFrontRightClosed",
    3: "doorRearLeftClosed",
    4: "doorRearRightClosed",
    5: "closureFrunkClosed",
    7: "closureLiftgateClosed",
    12: "windowFrontLeftClosed",
    13: "windowFrontRightClosed",
    14: "windowRearLeftClosed",
    15: "windowRearRightClosed",
    16: "windowLiftgateClosed",
}

R2_GEAR_STATES = {
    1: "Park",
    2: "Reverse",
    3: "Neutral",
    4: "Drive",
}

R2_DRIVE_MODES = {
    2: "All-Purpose",
    4: "Rally",
    8: "Sport",
    9: "Conserve",
    11: "All-Terrain",
    12: "Soft Sand",
    15: "Snow",
}

R2_BATTERY_SENSOR_KEYS = {
    "battery_cell_average_temperature",
    "battery_cell_max_temperature",
    "battery_cell_min_temperature",
}

R2_BINARY_SENSOR_KEYS = {
    "cabin_preconditioning_status",
    "charger_state",
    "charger_status",
    "closure_frunk_closed",
    "closure_liftgate_closed",
    "door_front_left_closed",
    "door_front_right_closed",
    "door_rear_left_closed",
    "door_rear_right_closed",
    "locked_state",
    "window_front_left_closed",
    "window_front_right_closed",
    "window_liftgate_closed",
    "window_rear_left_closed",
    "window_rear_right_closed",
}

R2_CHARGING_SENSOR_KEYS = {
    "charging_energy_delivered",
    "charging_energy_outlets",
    "charging_energy_system",
    "charging_energy_thermal",
    "charging_energy_to_pack",
    "charging_range_added",
    "charging_rate",
    "charging_session_time_remaining",
    "charging_speed",
    "charging_start_time",
    "charging_status",
    "charging_time_elapsed",
}

R2_OBSOLETE_ENTITY_KEYS = {
    "alarm",
    "battery_energy",
    "cabin_climate",
    "charge_port",
    "charge_limit",
    "charging_cost",
    "charging_enabled",
    "closures",
    "drop_tailgate",
    "front_trunk",
    "frunk",
    "gear_guard_video",
    "liftgate",
    "open_gear_tunnel_left",
    "open_gear_tunnel_right",
    "pair",
    "seat_front_left_heat",
    "seat_front_left_vent",
    "seat_front_right_heat",
    "seat_front_right_vent",
    "seat_rear_left_heat",
    "seat_rear_right_heat",
    "steering_wheel_heat",
    "time_to_end_of_charge",
    "tonneau",
    "wake",
    "windows",
}


def is_r2_vehicle(vehicle: dict[str, Any]) -> bool:
    """Return whether a vehicle is the exact R2 model."""
    return vehicle.get("model") == R2_MODEL


def supports_vehicle_control(vehicle: dict[str, Any]) -> bool:
    """Return whether the established R1 command path is enabled."""
    return not is_r2_vehicle(vehicle) and bool(vehicle.get("phone_identity_id"))


def r2_parallax_rvms(vehicle: dict[str, Any]) -> set[str]:
    """Return only Parallax topics advertised by the vehicle's feature flags."""
    supported_features = set(vehicle.get("supported_features", []))
    return {
        rvm
        for feature, rvms in R2_PARALLAX_RVMS_BY_FEATURE.items()
        if feature in supported_features
        for rvm in rvms
    }


class ObservationValidity(StrEnum):
    """Validity of an R2 observation."""

    CURRENT = "current"
    CLEARED = "cleared"


@dataclass(frozen=True, slots=True)
class R2Observation:
    """One presence-aware observation from a Rivian telemetry source."""

    value: Any
    source: str
    received_at: datetime
    source_timestamp_ms: int | None = None
    presence: bool = True
    validity: ObservationValidity = ObservationValidity.CURRENT

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        """Return a diagnostics-safe representation."""
        now = now or datetime.now(timezone.utc)
        return {
            "value": self.value,
            "source": self.source,
            "source_timestamp_ms": self.source_timestamp_ms,
            "received_at": self.received_at.isoformat(),
            "age_seconds": max(0.0, (now - self.received_at).total_seconds()),
            "presence": self.presence,
            "validity": self.validity,
        }


class R2ObservationStore:
    """Merge R2 telemetry without confusing absent values with zero or false."""

    def __init__(self) -> None:
        self._observations: dict[str, R2Observation] = {}

    def update(
        self,
        field: str,
        value: Any,
        *,
        source: str,
        source_timestamp_ms: int | None = None,
        received_at: datetime | None = None,
    ) -> bool:
        """Store a field that was explicitly present in a source message."""
        received_at = received_at or datetime.now(timezone.utc)
        if self._reject_update(field, source_timestamp_ms, received_at):
            return False
        self._observations[field] = R2Observation(
            value=value,
            source=source,
            source_timestamp_ms=source_timestamp_ms,
            received_at=received_at,
        )
        return True

    def clear(
        self,
        field: str,
        *,
        source: str,
        source_timestamp_ms: int | None = None,
        received_at: datetime | None = None,
    ) -> bool:
        """Explicitly clear a field while retaining why it was cleared."""
        received_at = received_at or datetime.now(timezone.utc)
        if self._reject_update(field, source_timestamp_ms, received_at):
            return False
        self._observations[field] = R2Observation(
            value=None,
            source=source,
            source_timestamp_ms=source_timestamp_ms,
            received_at=received_at,
            presence=False,
            validity=ObservationValidity.CLEARED,
        )
        return True

    def lifecycle_reset(
        self,
        field: str,
        value: Any,
        *,
        presence: bool,
        source: str,
        source_timestamp_ms: int | None,
        received_at: datetime,
    ) -> None:
        """Apply an authoritative lifecycle reset after its frame is ordered."""
        self._observations[field] = R2Observation(
            value=value,
            source=source,
            source_timestamp_ms=source_timestamp_ms,
            received_at=received_at,
            presence=presence,
            validity=(
                ObservationValidity.CURRENT if presence else ObservationValidity.CLEARED
            ),
        )

    def _reject_update(
        self,
        field: str,
        source_timestamp_ms: int | None,
        received_at: datetime,
    ) -> bool:
        """Reject older, unknown-order, and duplicate observations."""
        current = self._observations.get(field)
        if current is None:
            return False
        if current.source_timestamp_ms is not None:
            return (
                source_timestamp_ms is None
                or source_timestamp_ms <= current.source_timestamp_ms
            )
        if source_timestamp_ms is not None:
            return False
        return received_at <= current.received_at

    def get(self, field: str) -> Any | None:
        """Return the latest valid value for a field."""
        observation = self._observations.get(field)
        if observation is None or observation.validity is ObservationValidity.CLEARED:
            return None
        return observation.value

    def get_observation(self, field: str) -> R2Observation | None:
        """Return the full observation for a field."""
        return self._observations.get(field)

    def diagnostics(self) -> dict[str, dict[str, Any]]:
        """Return source, timestamp, age, validity, and value for every field."""
        now = datetime.now(timezone.utc)
        return {
            field: observation.as_dict(now)
            for field, observation in sorted(self._observations.items())
        }
