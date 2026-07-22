"""Exact-R2 Parallax coordinators for the Rivian integration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import logging
from typing import Any, cast

from rivian import (
    ParallaxMessage,
    Rivian,
    decode_charging_graph_global,
    decode_charging_session_live_data,
    decode_charging_session_status,
    decode_charging_time_estimation,
    decode_parallax_message,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    DRIVE_MODE_MAP,
    INVALID_SENSOR_STATES,
    VEHICLE_STATE_API_FIELDS,
)
from .coordinator import VehicleCoordinator
from .r2 import (
    R2_CLOSURE_FIELDS,
    R2_DRIVE_MODES,
    R2_GEAR_STATES,
    R2ObservationStore,
    r2_parallax_rvms,
)

_LOGGER = logging.getLogger(__name__)


class R2ChargingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push-only R2 charging coordinator backed by Parallax telemetry."""

    _SESSION_NUMERIC_FIELDS = {
        "kilometersChargedPerHour",
        "outletsChargedEnergy",
        "packChargedEnergy",
        "power",
        "rangeAddedThisSession",
        "systemChargedEnergy",
        "thermalChargedEnergy",
        "timeElapsed",
        "timeRemaining",
        "totalChargedEnergy",
    }
    _TRANSIENT_FIELDS = {
        "kilometersChargedPerHour",
        "power",
        "timeRemaining",
    }
    _TRANSIENT_TTL_SECONDS = 90

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, client: Rivian
    ) -> None:
        """Initialize the R2 charging coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_r2_charging",
            update_interval=None,
            always_update=False,
        )
        self.api = client
        self.data: dict[str, Any] = {}
        self.observations = R2ObservationStore()
        self.last_message_at: datetime | None = None
        self.last_message_by_rvm: dict[str, datetime] = {}
        self._plug_connection_status_code: int | None = None
        self._display_status_code: int | None = None
        self._evse_type_code: int | None = None
        self._telemetry_fresh = False
        self._freshness_timer: asyncio.TimerHandle | None = None
        self.state_callback: Callable[[dict[str, Any]], None] | None = None

    @property
    def is_active(self) -> bool:
        """Return whether the status topic reports active charging."""
        return (
            self._telemetry_fresh
            and self._plug_connection_status_code == 2
            and self._display_status_code == 3
        )

    def adjust_update_interval(self, is_plugged_in: bool) -> None:
        """Ignore legacy polling hints because R2 charging is push-only."""

    def _record(
        self,
        field: str,
        value: Any,
        *,
        rvm: str,
        timestamp_ms: int | None,
        received_at: datetime,
    ) -> None:
        """Record one explicitly present charging field."""
        accepted = self.observations.update(
            field,
            value,
            source=f"parallax:{rvm}",
            source_timestamp_ms=timestamp_ms,
            received_at=received_at,
        )
        if accepted:
            self.data[field] = value

    def _clear_session(
        self, *, rvm: str, timestamp_ms: int | None, received_at: datetime
    ) -> None:
        """Reset numeric session values and clear the session-only timestamp."""
        for field in self._SESSION_NUMERIC_FIELDS:
            self.observations.lifecycle_reset(
                field,
                0,
                presence=True,
                source=f"parallax:{rvm}:lifecycle_reset",
                source_timestamp_ms=timestamp_ms,
                received_at=received_at,
            )
            self.data[field] = 0
        self.observations.lifecycle_reset(
            "startTime",
            None,
            presence=False,
            source=f"parallax:{rvm}:lifecycle_reset",
            source_timestamp_ms=timestamp_ms,
            received_at=received_at,
        )
        self.data.pop("startTime", None)
        self._cancel_freshness_timer()

    def _initialize_session_defaults(
        self, *, rvm: str, timestamp_ms: int | None, received_at: datetime
    ) -> None:
        """Initialize unobserved session values with meaningful zeroes."""
        for field in self._SESSION_NUMERIC_FIELDS:
            observation = self.observations.get_observation(field)
            if observation is not None and observation.presence:
                continue
            self.observations.lifecycle_reset(
                field,
                0,
                presence=True,
                source=f"parallax:{rvm}:session_default",
                source_timestamp_ms=timestamp_ms,
                received_at=received_at,
            )
            self.data[field] = 0

    def _publish(self) -> None:
        """Publish charging data and synchronize vehicle-level binary state."""
        current_data = dict(self.data)
        self.async_set_updated_data(current_data)
        if self.state_callback is not None:
            self.state_callback(current_data)

    def _accept_frame(
        self,
        frame_key: str,
        message: ParallaxMessage,
        received_at: datetime,
    ) -> bool:
        """Reject delayed or duplicate frames before lifecycle mutation."""
        return self.observations.update(
            frame_key,
            message.timestamp_ms,
            source=f"parallax:{message.rvm}",
            source_timestamp_ms=message.timestamp_ms,
            received_at=received_at,
        )

    @callback
    def process_status(self, message: ParallaxMessage) -> None:
        """Apply charge connection and display state to the session lifecycle."""
        status = decode_charging_session_status(message.payload)
        received_at = datetime.now(timezone.utc)
        self._note_message(message.rvm, received_at)
        if (
            status.plug_connection_status_code is None
            and status.display_status_code is None
            and status.evse_type_code is None
        ) or not self._accept_frame(
            "_statusFrame",
            message,
            received_at,
        ):
            return

        if status.plug_connection_status_code is not None:
            self._plug_connection_status_code = status.plug_connection_status_code
        if status.display_status_code is not None:
            self._display_status_code = status.display_status_code
        if status.evse_type_code is not None:
            self._evse_type_code = status.evse_type_code

        status_names: dict[tuple[int | None, int | None], str] = {
            (1, 1): "unplugged",
            (2, 3): "charging",
            (2, 4): "complete",
            (2, 5): "scheduled",
            (2, 8): "stopped",
        }
        status_name = status_names.get(
            (self._plug_connection_status_code, self._display_status_code),
            "unknown",
        )
        self._record(
            "sessionStatus",
            status_name,
            rvm=message.rvm,
            timestamp_ms=message.timestamp_ms,
            received_at=received_at,
        )

        if status_name == "unknown":
            self._telemetry_fresh = False
            for field in ("isPluggedIn", "isCharging", *self._TRANSIENT_FIELDS):
                self.observations.lifecycle_reset(
                    field,
                    None,
                    presence=False,
                    source=f"parallax:{message.rvm}:unknown_status",
                    source_timestamp_ms=message.timestamp_ms,
                    received_at=received_at,
                )
                self.data.pop(field, None)
            self._cancel_freshness_timer()
            self._publish()
            return

        self._telemetry_fresh = status_name == "charging"
        self._record(
            "isPluggedIn",
            status_name in {"charging", "complete", "scheduled", "stopped"},
            rvm=message.rvm,
            timestamp_ms=message.timestamp_ms,
            received_at=received_at,
        )
        self._record(
            "isCharging",
            status_name == "charging",
            rvm=message.rvm,
            timestamp_ms=message.timestamp_ms,
            received_at=received_at,
        )

        if status_name in {"unplugged", "scheduled"}:
            self._clear_session(
                rvm=message.rvm,
                timestamp_ms=message.timestamp_ms,
                received_at=received_at,
            )
        elif status_name in {"complete", "stopped"}:
            # R2 retains its last non-zero rate and charge-state enum after a
            # session stops. The status topic is authoritative for live power.
            for field in self._TRANSIENT_FIELDS:
                self.observations.lifecycle_reset(
                    field,
                    0,
                    presence=True,
                    source=f"parallax:{message.rvm}:lifecycle_reset",
                    source_timestamp_ms=message.timestamp_ms,
                    received_at=received_at,
                )
                self.data[field] = 0
            self._cancel_freshness_timer()
        else:
            self._initialize_session_defaults(
                rvm=message.rvm,
                timestamp_ms=message.timestamp_ms,
                received_at=received_at,
            )
            self._schedule_freshness_expiry()
        self._publish()

    @callback
    def process_live_data(self, message: ParallaxMessage) -> None:
        """Apply fields present in the R2 charge-session breakdown."""
        live_data = decode_charging_session_live_data(message.payload)
        received_at = datetime.now(timezone.utc)
        self._note_message(message.rvm, received_at)
        if not self._accept_frame("_liveFrame", message, received_at):
            return
        if self._display_status_code == 5 or self._plug_connection_status_code == 1:
            return
        if self._plug_connection_status_code == 2 and self._display_status_code == 3:
            self._telemetry_fresh = True
        values = {
            "totalChargedEnergy": live_data.total_kwh,
            "packChargedEnergy": live_data.pack_kwh,
            "thermalChargedEnergy": live_data.thermal_kwh,
            "outletsChargedEnergy": live_data.outlets_kwh,
            "systemChargedEnergy": live_data.system_kwh,
            "rangeAddedThisSession": live_data.range_added_km,
            "timeElapsed": (
                live_data.session_duration_minutes * 60
                if live_data.session_duration_minutes is not None
                else None
            ),
        }
        if self.is_active:
            values.update(
                {
                    "kilometersChargedPerHour": live_data.current_range_km_per_hour,
                    "power": live_data.current_power_kw,
                    "timeRemaining": (
                        live_data.time_remaining_minutes * 60
                        if live_data.time_remaining_minutes is not None
                        else None
                    ),
                }
            )
        for field, value in values.items():
            if value is not None:
                self._record(
                    field,
                    value,
                    rvm=message.rvm,
                    timestamp_ms=message.timestamp_ms,
                    received_at=received_at,
                )
        if self.is_active:
            self._schedule_freshness_expiry()
        self._publish()

    @callback
    def process_time_estimation(self, message: ParallaxMessage) -> None:
        """Apply the direct R2 remaining-time estimate while charging."""
        estimation = decode_charging_time_estimation(message.payload)
        received_at = datetime.now(timezone.utc)
        self._note_message(message.rvm, received_at)
        if not self._accept_frame("_timeEstimationFrame", message, received_at):
            return
        if self._display_status_code == 5 or self._plug_connection_status_code == 1:
            return
        if self._plug_connection_status_code == 2 and self._display_status_code == 3:
            self._telemetry_fresh = True
        if self.is_active and estimation.estimated_minutes_remaining is not None:
            self._record(
                "timeRemaining",
                estimation.estimated_minutes_remaining * 60,
                rvm=message.rvm,
                timestamp_ms=message.timestamp_ms,
                received_at=received_at,
            )
            self._schedule_freshness_expiry()
            self._publish()

    @callback
    def process_graph(self, message: ParallaxMessage) -> None:
        """Use the earliest valid graph bar as the direct session start time."""
        graph = decode_charging_graph_global(message.payload)
        received_at = datetime.now(timezone.utc)
        self._note_message(message.rvm, received_at)
        if not self._accept_frame("_chargingGraphFrame", message, received_at):
            return
        if self._display_status_code == 5 or self._plug_connection_status_code == 1:
            return
        start_times = [
            bar.start_time_ms
            for bar in graph.bars
            if bar.start_time_ms is not None and bar.start_time_ms > 0
        ]
        if start_times and self._plug_connection_status_code != 1:
            start_time = datetime.fromtimestamp(
                min(start_times) / 1000, timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%f%z")
            self._record(
                "startTime",
                start_time,
                rvm=message.rvm,
                timestamp_ms=message.timestamp_ms,
                received_at=received_at,
            )
            self._publish()

    @callback
    def _expire_transient_fields(self) -> None:
        """Expire live values when an active R2 stream stops updating."""
        self._freshness_timer = None
        received_at = datetime.now(timezone.utc)
        self._telemetry_fresh = False
        for field in self._TRANSIENT_FIELDS:
            observation = self.observations.get_observation(field)
            self.observations.lifecycle_reset(
                field,
                None,
                presence=False,
                source="freshness_timeout",
                source_timestamp_ms=(
                    observation.source_timestamp_ms if observation else None
                ),
                received_at=received_at,
            )
            self.data.pop(field, None)
        charging_observation = self.observations.get_observation("isCharging")
        status_pair = (
            self._plug_connection_status_code,
            self._display_status_code,
        )
        charging_known = status_pair in {(1, 1), (2, 3), (2, 4), (2, 5), (2, 8)}
        self.observations.lifecycle_reset(
            "isCharging",
            False if charging_known else None,
            presence=charging_known,
            source="freshness_timeout",
            source_timestamp_ms=(
                charging_observation.source_timestamp_ms
                if charging_observation
                else None
            ),
            received_at=received_at,
        )
        status_observation = self.observations.get_observation("sessionStatus")
        self.observations.lifecycle_reset(
            "sessionStatus",
            "unknown",
            presence=True,
            source="freshness_timeout",
            source_timestamp_ms=(
                status_observation.source_timestamp_ms if status_observation else None
            ),
            received_at=received_at,
        )
        if charging_known:
            self.data["isCharging"] = False
        else:
            self.data.pop("isCharging", None)
        self.data["sessionStatus"] = "unknown"
        self._publish()

    def _schedule_freshness_expiry(self) -> None:
        """Schedule transient field expiry for three normal update cadences."""
        self._cancel_freshness_timer()
        self._freshness_timer = self.hass.loop.call_later(
            self._TRANSIENT_TTL_SECONDS, self._expire_transient_fields
        )

    def _cancel_freshness_timer(self) -> None:
        """Cancel the pending freshness callback."""
        if self._freshness_timer is not None:
            self._freshness_timer.cancel()
            self._freshness_timer = None

    async def async_shutdown(self) -> None:
        """Cancel local timers during config-entry unload."""
        self._cancel_freshness_timer()
        await super().async_shutdown()

    def _note_message(self, rvm: str, received_at: datetime) -> None:
        """Record subscription health without retaining raw payloads."""
        self.last_message_at = received_at
        self.last_message_by_rvm[rvm] = received_at

    def diagnostics(self) -> dict[str, Any]:
        """Return sanitized R2 charging and subscription diagnostics."""
        return {
            "active": self.is_active,
            "plug_connection_status_code": self._plug_connection_status_code,
            "display_status_code": self._display_status_code,
            "evse_type_code": self._evse_type_code,
            "last_message_at": (
                self.last_message_at.isoformat() if self.last_message_at else None
            ),
            "last_message_by_rvm": {
                rvm: timestamp.isoformat()
                for rvm, timestamp in sorted(self.last_message_by_rvm.items())
            },
            "observations": self.observations.diagnostics(),
        }


class R2VehicleCoordinator(VehicleCoordinator):
    """Exact-R2 coordinator that augments legacy state with Parallax telemetry."""

    _NAVIGATION_FIELDS = (
        "navigationDestination",
        "navigationDestinationLatitude",
        "navigationDestinationLongitude",
        "navigationDistanceRemaining",
        "navigationEta",
        "navigationTimeRemaining",
        "navigationTripId",
    )
    _NAVIGATION_TRANSIENT_TTL_SECONDS = 20

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: Rivian,
        vehicle: dict[str, Any],
    ) -> None:
        """Initialize the R2 coordinator and stable feature profile."""
        super().__init__(
            hass=hass,
            config_entry=config_entry,
            client=client,
            vehicle_id=vehicle["id"],
        )
        self.r2_charging_coordinator = R2ChargingCoordinator(
            hass=hass, config_entry=config_entry, client=client
        )
        self.r2_charging_coordinator.state_callback = self._process_r2_charging_state
        self.charging_coordinator = cast(Any, self.r2_charging_coordinator)
        self.data: dict[str, Any] = {}
        self.observations = R2ObservationStore()
        self.parallax_rvms = r2_parallax_rvms(vehicle)
        self.last_parallax_message_at: datetime | None = None
        self.last_parallax_message_by_rvm: dict[str, datetime] = {}
        self._parallax_unsubscribe: Callable[[], Awaitable[None]] | None = None
        self._unsub_handler: Callable[[], Awaitable[None]] | None = None
        self._navigation_freshness_timer: asyncio.TimerHandle | None = None
        self._error_count = 0

    async def _async_update_data(self) -> dict[str, Any]:
        """Subscribe to advertised R2 topics and retain the legacy state feed."""
        if self.parallax_rvms and self._parallax_unsubscribe is None:
            unsubscribe = await self.api.subscribe_for_parallax_messages(
                vehicle_id=self.vehicle_id,
                rvms=self.parallax_rvms,
                callback=self._process_parallax_message,
            )
            if unsubscribe is None:
                raise UpdateFailed("Could not subscribe to R2 Parallax telemetry")
            self._parallax_unsubscribe = unsubscribe
        if self._unsub_handler is None:
            unsubscribe = await self.api.subscribe_for_vehicle_updates(
                vehicle_id=self.vehicle_id,
                properties=VEHICLE_STATE_API_FIELDS,
                callback=self._process_new_data,
            )
            if unsubscribe is None:
                raise UpdateFailed("Could not subscribe to R2 legacy telemetry")
            self._unsub_handler = unsubscribe
        # Both subscription methods wait for the WebSocket acknowledgement.
        # An asleep R2 is healthy even when it emits no fresh state frame.
        return self.data or {}

    @callback
    def _process_new_data(self, data: dict[str, Any]) -> None:
        """Merge explicit legacy values into the R2 observation store."""
        if not (payload := data.get("payload")) or not (pdata := payload.get("data")):
            _LOGGER.error("Received an unknown R2 legacy subscription update")
            self._error_count += 1
            return
        previous = dict(self.data or {})
        incoming = pdata.get(self.key, {})
        usable_incoming = {
            field: entity
            | {"value": self._normalize_legacy_value(field, entity["value"])}
            for field, entity in incoming.items()
            if isinstance(entity, dict)
            and "value" in entity
            and entity["value"] is not None
            and not (
                isinstance(entity["value"], str)
                and entity["value"].lower() in INVALID_SENSOR_STATES
            )
        }
        vehicle_info = self._build_vehicle_info_dict(usable_incoming)
        for field, entity in usable_incoming.items():
            if not isinstance(entity, dict) or "value" not in entity:
                continue
            timestamp_ms = self._legacy_timestamp_ms(entity.get("timeStamp"))
            if (
                not self.observations.update(
                    field,
                    entity["value"],
                    source="vehicleState",
                    source_timestamp_ms=timestamp_ms,
                )
                and field in previous
            ):
                vehicle_info[field] = previous[field]
        self.async_set_updated_data(vehicle_info)
        self._error_count = 0
        self._initial.set()

    @staticmethod
    def _normalize_legacy_value(field: str, value: Any) -> Any:
        """Normalize R2 enum aliases from the supplemental legacy snapshot."""
        if not isinstance(value, str):
            return value
        if field == "driveMode":
            normalized = DRIVE_MODE_MAP.get(value, value)
            return normalized if normalized in R2_DRIVE_MODES.values() else "unknown"
        if field == "gearStatus":
            normalized = value.title()
            return normalized if normalized in R2_GEAR_STATES.values() else "unknown"
        return value

    @callback
    def _process_parallax_message(self, message: ParallaxMessage) -> None:
        """Route an R2 message without logging its raw protobuf payload."""
        received_at = datetime.now(timezone.utc)
        self.last_parallax_message_at = received_at
        self.last_parallax_message_by_rvm[message.rvm] = received_at
        try:
            if message.rvm == "charging.session.status":
                self.r2_charging_coordinator.process_status(message)
            elif message.rvm == "charging.session.time_estimation":
                self.r2_charging_coordinator.process_time_estimation(message)
            elif message.rvm == "energy_edge_compute.graphs.charge_session_breakdown":
                self.r2_charging_coordinator.process_live_data(message)
            elif message.rvm == "energy_edge_compute.graphs.charging_graph_global":
                self.r2_charging_coordinator.process_graph(message)
            else:
                self._apply_parallax_state(message, decode_parallax_message(message))
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Could not decode R2 topic %s: %s", message.rvm, err)

    def _apply_parallax_state(self, message: ParallaxMessage, decoded: Any) -> None:
        """Adapt typed Parallax state to stable Home Assistant field semantics."""
        values: dict[str, Any] = {}
        source_timestamps: dict[str, int] = {}
        match message.rvm:
            case "energy.high_voltage.battery_state":
                values = {
                    "batteryLevel": decoded.soc_percent,
                    "batteryCapacity": decoded.capacity_kwh,
                    "distanceToEmpty": decoded.range_km,
                    "batteryCellAverageTemperature": decoded.cell_average_c,
                    "batteryCellMaxTemperature": decoded.cell_max_c,
                    "batteryCellMinTemperature": decoded.cell_min_c,
                    "r2BatteryPowerOutputCode": decoded.power_output_code,
                    "r2BatteryRequiresCalibration": decoded.requires_calibration,
                    "r2BatteryColdWeatherStateCode": decoded.cold_weather_state_code,
                }
            case "dynamics.vehicle.range":
                values = {"distanceToEmpty": decoded.distance_km}
            case "dynamics.vehicle.odometer":
                values = {
                    "vehicleMileage": (
                        decoded.distance_km * 1000
                        if decoded.distance_km is not None
                        else None
                    )
                }
            case "dynamics.vehicle.gnss":
                if decoded.latitude is not None and decoded.longitude is not None:
                    values["gnssLocation"] = {
                        "latitude": decoded.latitude,
                        "longitude": decoded.longitude,
                    }
                values["gnssAltitude"] = decoded.altitude_m
                values["gnssSpeed"] = decoded.speed_m_s
                values["gnssBearing"] = (
                    decoded.heading_deg % 360
                    if decoded.heading_deg is not None
                    else None
                )
                for field, value in {
                    "gnssSpeed": decoded.speed_m_s,
                    "gnssBearing": decoded.heading_deg,
                }.items():
                    if value is None:
                        self._clear_observation(
                            field,
                            source=f"parallax:{message.rvm}:absent",
                            source_timestamp_ms=(
                                decoded.timestamp_ms or message.timestamp_ms
                            ),
                        )
                if decoded.timestamp_ms is not None:
                    source_timestamps.update(
                        {
                            "gnssLocation": decoded.timestamp_ms,
                            "gnssAltitude": decoded.timestamp_ms,
                            "gnssSpeed": decoded.timestamp_ms,
                            "gnssBearing": decoded.timestamp_ms,
                        }
                    )
            case "vehicle.power.state":
                values = {
                    "powerState": {1: "sleep", 3: "ready", 4: "go"}.get(
                        decoded.state_code,
                        "unknown" if decoded.state_code is not None else None,
                    ),
                    "r2PowerStateCode": decoded.state_code,
                }
            case "dynamics.vehicle.gear":
                values = {
                    "gearStatus": (
                        R2_GEAR_STATES.get(decoded.state_code, "unknown")
                        if decoded.state_code is not None
                        else None
                    ),
                    "r2GearStateCode": decoded.state_code,
                }
            case "dynamics.vehicle.drive_mode":
                values = {
                    "driveMode": (
                        R2_DRIVE_MODES.get(decoded.mode_code, "unknown")
                        if decoded.mode_code is not None
                        else None
                    ),
                    "r2DriveModeCode": decoded.mode_code,
                }
            case "dynamics.tires.state":
                values = {"r2TireMonitorStatusCode": decoded.monitor_status_code}
                pressure_fields = {
                    1: "tirePressureFrontLeft",
                    2: "tirePressureFrontRight",
                    3: "tirePressureRearLeft",
                    4: "tirePressureRearRight",
                }
                for tire in decoded.tires:
                    if tire.position_code is None:
                        continue
                    values.update(
                        {
                            f"r2Tire{tire.position_code}Pressure": tire.pressure_bar,
                            f"r2Tire{tire.position_code}StatusCode": tire.status_code,
                            f"r2Tire{tire.position_code}ValidityCode": tire.validity_code,
                        }
                    )
                    if pressure_field := pressure_fields.get(tire.position_code):
                        values[pressure_field] = tire.pressure_bar
                    if tire.timestamp_ms is not None:
                        source_timestamps.update(
                            {
                                f"r2Tire{tire.position_code}Pressure": tire.timestamp_ms,
                                f"r2Tire{tire.position_code}StatusCode": tire.timestamp_ms,
                                f"r2Tire{tire.position_code}ValidityCode": tire.timestamp_ms,
                            }
                        )
                        if pressure_field:
                            source_timestamps[pressure_field] = tire.timestamp_ms
            case "body.closures.states":
                for state in decoded.states:
                    if state.position_code is not None and state.state_code is not None:
                        values[f"r2Closure{state.position_code}StateCode"] = (
                            state.state_code
                        )
                    field = R2_CLOSURE_FIELDS.get(state.position_code)
                    if field is None:
                        continue
                    closure_state = (
                        "open"
                        if state.state_code in {1, 3, 4, 5}
                        else "closed"
                        if state.state_code == 2
                        else None
                    )
                    if closure_state is None:
                        self._clear_observation(
                            field,
                            source=f"parallax:{message.rvm}",
                            source_timestamp_ms=message.timestamp_ms,
                        )
                    else:
                        values[field] = closure_state
            case "body.locks.states":
                observed_positions = {
                    state.position_code
                    for state in decoded.states
                    if state.position_code is not None
                }
                for state in decoded.states:
                    if state.position_code is not None and state.state_code is not None:
                        values[f"r2Lock{state.position_code}StateCode"] = (
                            state.state_code
                        )
                if (
                    len(decoded.states) == 6
                    and observed_positions
                    == {
                        1,
                        2,
                        3,
                        4,
                        5,
                        7,
                    }
                    and all(state.state_code == 1 for state in decoded.states)
                ):
                    values["r2AllLocked"] = True
                elif (
                    len(decoded.states) == 6
                    and observed_positions == {1, 2, 3, 4, 5, 7}
                    and all(state.state_code in {1, 2, 3} for state in decoded.states)
                ):
                    values["r2AllLocked"] = all(
                        state.state_code == 1 for state in decoded.states
                    )
                else:
                    self._clear_observation(
                        "r2AllLocked",
                        source=f"parallax:{message.rvm}",
                        source_timestamp_ms=message.timestamp_ms,
                    )
            case "body.windows.states":
                for window in decoded.states:
                    if window.position_code is not None:
                        values[f"r2Window{window.position_code}StateCode"] = (
                            window.state_code
                        )
            case "comfort.cabin.cabin_preconditioning_status":
                if decoded.status_code not in (None, 1, 2, 3, 4):
                    self._clear_observation(
                        "cabinPreconditioningStatus",
                        source=f"parallax:{message.rvm}",
                        source_timestamp_ms=message.timestamp_ms,
                    )
                values = {
                    "cabinPreconditioningStatus": "active"
                    if decoded.status_code in {1, 2, 3, 4}
                    else "inactive"
                    if decoded.status_code is None
                    else None,
                    "r2CabinPreconditioningStatusCode": decoded.status_code,
                    "r2CabinPreconditioningTypeCode": decoded.type_code,
                }
            case "comfort.cabin.cabin_temperatures":
                values = {"cabinClimateInteriorTemperature": decoded.interior_c}
            case "navigation.navigation_service.trip_progress":
                progress_timestamp_ms = (
                    decoded.motion.timestamp_ms
                    if decoded.motion is not None
                    else message.timestamp_ms
                )
                values = {
                    "navigationDistanceRemaining": decoded.remaining_distance_m,
                    "navigationTimeRemaining": decoded.remaining_drive_time_s,
                }
                if decoded.motion is not None:
                    if (
                        decoded.motion.latitude is not None
                        and decoded.motion.longitude is not None
                    ):
                        values["gnssLocation"] = {
                            "latitude": decoded.motion.latitude,
                            "longitude": decoded.motion.longitude,
                        }
                    values["gnssSpeed"] = decoded.motion.speed_m_s
                    values["gnssBearing"] = (
                        decoded.motion.heading_deg % 360
                        if decoded.motion.heading_deg is not None
                        else None
                    )
                    source_timestamps.update(
                        {
                            "gnssLocation": progress_timestamp_ms,
                            "gnssSpeed": progress_timestamp_ms,
                            "gnssBearing": progress_timestamp_ms,
                        }
                    )
                    for field, value in {
                        "gnssSpeed": decoded.motion.speed_m_s,
                        "gnssBearing": decoded.motion.heading_deg,
                    }.items():
                        if value is None:
                            self._clear_observation(
                                field,
                                source=f"parallax:{message.rvm}:absent",
                                source_timestamp_ms=progress_timestamp_ms,
                            )
                if any(value is not None for value in values.values()):
                    self._schedule_navigation_freshness_expiry()
                else:
                    self._clear_navigation_state(
                        source=f"parallax:{message.rvm}:empty",
                        source_timestamp_ms=message.timestamp_ms,
                    )
            case "navigation.navigation_service.trip_info":
                if decoded.trip_id is None:
                    self._clear_navigation_state(
                        source=f"parallax:{message.rvm}:ended",
                        source_timestamp_ms=message.timestamp_ms,
                    )
                else:
                    values = {
                        "navigationTripId": decoded.trip_id,
                        "navigationDestination": decoded.destination_name,
                        "navigationDestinationLatitude": (decoded.destination_latitude),
                        "navigationDestinationLongitude": (
                            decoded.destination_longitude
                        ),
                        "navigationEta": (
                            datetime.fromtimestamp(
                                decoded.eta_timestamp_ms / 1000, timezone.utc
                            )
                            if decoded.eta_timestamp_ms is not None
                            else None
                        ),
                    }
        for field, value in values.items():
            if value is not None:
                self._record_observation(
                    field,
                    value,
                    source=f"parallax:{message.rvm}",
                    source_timestamp_ms=source_timestamps.get(
                        field, message.timestamp_ms
                    ),
                )
        if values:
            self.async_set_updated_data(dict(self.data or {}))

    def _clear_navigation_state(
        self,
        *,
        source: str,
        source_timestamp_ms: int | None,
        received_at: datetime | None = None,
    ) -> None:
        """Clear active-route values while retaining the last vehicle position."""
        received_at = received_at or datetime.now(timezone.utc)
        for field in self._NAVIGATION_FIELDS:
            self.observations.lifecycle_reset(
                field,
                None,
                presence=False,
                source=source,
                source_timestamp_ms=source_timestamp_ms,
                received_at=received_at,
            )
            self.data.pop(field, None)
        for field in ("gnssSpeed", "gnssBearing"):
            observation = self.observations.get_observation(field)
            if observation is None or "trip_progress" not in observation.source:
                continue
            self.observations.lifecycle_reset(
                field,
                None,
                presence=False,
                source=source,
                source_timestamp_ms=source_timestamp_ms,
                received_at=received_at,
            )
            self.data.pop(field, None)
        self._cancel_navigation_freshness_timer()
        self.async_set_updated_data(dict(self.data))

    @callback
    def _expire_navigation_state(self) -> None:
        """Expire active-route values when expected progress frames stop."""
        self._navigation_freshness_timer = None
        self._clear_navigation_state(
            source="navigation_freshness_timeout",
            source_timestamp_ms=None,
        )

    def _schedule_navigation_freshness_expiry(self) -> None:
        """Expire navigation after four expected five-second update periods."""
        self._cancel_navigation_freshness_timer()
        self._navigation_freshness_timer = self.hass.loop.call_later(
            self._NAVIGATION_TRANSIENT_TTL_SECONDS,
            self._expire_navigation_state,
        )

    def _cancel_navigation_freshness_timer(self) -> None:
        """Cancel the pending active-route freshness callback."""
        if self._navigation_freshness_timer is not None:
            self._navigation_freshness_timer.cancel()
            self._navigation_freshness_timer = None

    def _record_observation(
        self,
        field: str,
        value: Any,
        *,
        source: str,
        source_timestamp_ms: int | None,
        received_at: datetime | None = None,
    ) -> None:
        """Record accepted R2 data in the legacy-compatible outward shape."""
        received_at = received_at or datetime.now(timezone.utc)
        if not self.observations.update(
            field,
            value,
            source=source,
            source_timestamp_ms=source_timestamp_ms,
            received_at=received_at,
        ):
            return
        timestamp = (
            datetime.fromtimestamp(source_timestamp_ms / 1000, timezone.utc)
            if source_timestamp_ms is not None
            else received_at
        ).isoformat()
        if field == "gnssLocation":
            self.data[field] = value | {"timeStamp": timestamp}
            return
        previous_history = self.data.get(field, {}).get("history", set())
        history = set(previous_history)
        if isinstance(value, str | int | float | bool):
            history.add(value)
        self.data[field] = {
            "value": value,
            "timeStamp": timestamp,
            "history": history,
        }

    def _clear_observation(
        self,
        field: str,
        *,
        source: str,
        source_timestamp_ms: int | None,
        received_at: datetime | None = None,
    ) -> None:
        """Clear an R2 field when a newer direct enum cannot be interpreted."""
        if self.observations.clear(
            field,
            source=source,
            source_timestamp_ms=source_timestamp_ms,
            received_at=received_at,
        ):
            self.data.pop(field, None)

    @callback
    def _process_r2_charging_state(self, data: dict[str, Any]) -> None:
        """Mirror R2 charging booleans onto the vehicle device coordinator."""
        for field in ("isPluggedIn", "isCharging"):
            observation = self.r2_charging_coordinator.observations.get_observation(
                field
            )
            if observation is None:
                continue
            if not observation.presence:
                self._clear_observation(
                    field,
                    source=observation.source,
                    source_timestamp_ms=observation.source_timestamp_ms,
                    received_at=observation.received_at,
                )
                continue
            if field not in data:
                continue
            self._record_observation(
                field,
                data[field],
                source=observation.source if observation else "r2_charging",
                source_timestamp_ms=(
                    observation.source_timestamp_ms if observation else None
                ),
                received_at=observation.received_at if observation else None,
            )
        self.async_set_updated_data(dict(self.data))

    @staticmethod
    def _legacy_timestamp_ms(value: Any) -> int | None:
        """Convert a legacy ISO timestamp to the common source-time ordering."""
        if not isinstance(value, str):
            return None
        try:
            return int(
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            return None

    def get(self, key: str) -> Any | None:
        """Return an R2 observation, falling back to legacy state shape."""
        if self.observations.get_observation(key) is not None:
            return self.observations.get(key)
        return super().get(key)

    def get_observation(self, key: str) -> Any | None:
        """Return presence-aware R2 metadata or a legacy observation wrapper."""
        return self.observations.get_observation(key) or super().get_observation(key)

    def is_field_available(self, key: str) -> bool:
        """Return whether an R2 field has a present, non-cleared value."""
        return self.get(key) is not None

    async def async_shutdown(self) -> None:
        """Unsubscribe the R2 stream before closing the shared monitor."""
        self._cancel_navigation_freshness_timer()
        if self._parallax_unsubscribe is not None:
            await self._parallax_unsubscribe()
            self._parallax_unsubscribe = None
        await self.r2_charging_coordinator.async_shutdown()
        await super().async_shutdown()

    def diagnostics(self) -> dict[str, Any]:
        """Return sanitized profile and subscription-health diagnostics."""
        return {
            "profile": "r2_parallax",
            "requested_rvms": sorted(self.parallax_rvms),
            "last_parallax_message_at": (
                self.last_parallax_message_at.isoformat()
                if self.last_parallax_message_at
                else None
            ),
            "last_parallax_message_by_rvm": {
                rvm: timestamp.isoformat()
                for rvm, timestamp in sorted(self.last_parallax_message_by_rvm.items())
            },
            "observations": self.observations.diagnostics(),
            "charging": self.r2_charging_coordinator.diagnostics(),
        }
