"""Tests for the R2 charging lifecycle."""

import asyncio
import json
from types import SimpleNamespace

from rivian import ParallaxMessage

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.rivian.coordinator import VehicleCoordinator
from custom_components.rivian.r2 import R2ObservationStore
from custom_components.rivian.r2_coordinator import (
    R2ChargingCoordinator,
    R2VehicleCoordinator,
)


class _Timer:
    """Minimal asyncio timer test double."""

    def __init__(self, callback) -> None:
        self.callback = callback
        self.cancelled = False

    def cancel(self) -> None:
        """Mark the timer as cancelled."""
        self.cancelled = True


class _Loop:
    """Minimal event-loop test double."""

    def __init__(self) -> None:
        self.timer: _Timer | None = None

    def call_later(self, delay: float, callback) -> _Timer:
        """Capture a delayed callback without waiting."""
        assert delay in {20, 90}
        self.timer = _Timer(callback)
        return self.timer


def _coordinator() -> R2ChargingCoordinator:
    """Build a coordinator without Home Assistant's scheduler."""
    coordinator = object.__new__(R2ChargingCoordinator)
    coordinator.data = {}
    coordinator.observations = R2ObservationStore()
    coordinator.last_message_at = None
    coordinator.last_message_by_rvm = {}
    coordinator._plug_connection_status_code = None
    coordinator._display_status_code = None
    coordinator._evse_type_code = None
    coordinator._telemetry_fresh = False
    coordinator._freshness_timer = None
    coordinator.state_callback = None
    coordinator.hass = SimpleNamespace(loop=_Loop())
    coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
    return coordinator


def _message(rvm: str, timestamp_ms: int) -> ParallaxMessage:
    return ParallaxMessage(rvm=rvm, timestamp_ms=timestamp_ms, payload=b"test")


def _vehicle_coordinator() -> R2VehicleCoordinator:
    """Build an R2 vehicle coordinator for typed-adapter unit tests."""
    coordinator = object.__new__(R2VehicleCoordinator)
    coordinator.data = {}
    coordinator.vehicle_id = "vehicle"
    coordinator.observations = R2ObservationStore()
    coordinator._error_count = 0
    coordinator._initial = asyncio.Event()
    coordinator._awake = asyncio.Event()
    coordinator.charging_coordinator = SimpleNamespace(
        adjust_update_interval=lambda **kwargs: None
    )
    coordinator._navigation_freshness_timer = None
    coordinator.hass = SimpleNamespace(loop=_Loop())
    coordinator.async_set_updated_data = lambda data: setattr(coordinator, "data", data)
    return coordinator


def test_active_complete_and_unplugged_lifecycle(monkeypatch) -> None:
    """Live values zero on completion, totals persist, and unplug clears them."""
    coordinator = _coordinator()
    statuses = iter(
        (
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=3,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=4,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=1,
                display_status_code=1,
                evse_type_code=None,
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: next(statuses),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=2.2,
            pack_kwh=2.1,
            thermal_kwh=0.0,
            outlets_kwh=0.0,
            system_kwh=0.1,
            session_duration_minutes=13,
            time_remaining_minutes=82,
            range_added_km=10,
            current_power_kw=10.7,
            current_range_km_per_hour=60,
        ),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 110)
    )
    assert coordinator.data["isPluggedIn"] is True
    assert coordinator.data["isCharging"] is True
    assert coordinator.data["power"] == 10.7
    assert coordinator.data["totalChargedEnergy"] == 2.2

    coordinator.process_status(_message("charging.session.status", 120))
    assert coordinator.data["sessionStatus"] == "complete"
    assert coordinator.data["power"] == 0
    assert coordinator.data["kilometersChargedPerHour"] == 0
    assert coordinator.data["totalChargedEnergy"] == 2.2

    coordinator.process_status(_message("charging.session.status", 130))
    assert coordinator.data["isCharging"] is False
    assert coordinator.data["isPluggedIn"] is False
    assert coordinator.data["sessionStatus"] == "unplugged"
    assert coordinator.data["power"] == 0
    assert coordinator.data["totalChargedEnergy"] == 0
    assert "startTime" not in coordinator.data


def test_scheduled_status_initializes_zeroes_and_ignores_stale_session_topics(
    monkeypatch,
) -> None:
    """A scheduled charge is plugged in but has no active session values."""
    coordinator = _coordinator()
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: SimpleNamespace(
            plug_connection_status_code=2,
            display_status_code=5,
            evse_type_code=1,
        ),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=2.2,
            pack_kwh=2.1,
            thermal_kwh=0.0,
            outlets_kwh=0.0,
            system_kwh=0.1,
            session_duration_minutes=13,
            time_remaining_minutes=82,
            range_added_km=10,
            current_power_kw=10.7,
            current_range_km_per_hour=60,
        ),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_time_estimation",
        lambda payload: SimpleNamespace(estimated_minutes_remaining=82),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_graph_global",
        lambda payload: SimpleNamespace(bars=(SimpleNamespace(start_time_ms=1_000),)),
    )

    coordinator.process_status(_message("charging.session.status", 100))

    assert coordinator.data["sessionStatus"] == "scheduled"
    assert coordinator.data["isPluggedIn"] is True
    assert coordinator.data["isCharging"] is False
    assert all(
        coordinator.data[field] == 0 for field in coordinator._SESSION_NUMERIC_FIELDS
    )
    assert "startTime" not in coordinator.data

    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 200)
    )
    coordinator.process_time_estimation(
        _message("charging.session.time_estimation", 210)
    )
    coordinator.process_graph(
        _message("energy_edge_compute.graphs.charging_graph_global", 220)
    )

    assert coordinator.data["totalChargedEnergy"] == 0
    assert coordinator.data["power"] == 0
    assert coordinator.data["timeRemaining"] == 0
    assert "startTime" not in coordinator.data


def test_scheduled_to_active_retains_zeroes_until_fresh_values_arrive(
    monkeypatch,
) -> None:
    """An active session starts cleanly when its first energy totals are omitted."""
    coordinator = _coordinator()
    statuses = iter(
        (
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=5,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=3,
                evse_type_code=1,
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: next(statuses),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=None,
            pack_kwh=None,
            thermal_kwh=None,
            outlets_kwh=None,
            system_kwh=None,
            session_duration_minutes=1,
            time_remaining_minutes=103,
            range_added_km=None,
            current_power_kw=11.1,
            current_range_km_per_hour=61,
        ),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_graph_global",
        lambda payload: SimpleNamespace(bars=(SimpleNamespace(start_time_ms=1_000),)),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator.process_status(_message("charging.session.status", 200))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 210)
    )
    coordinator.process_graph(
        _message("energy_edge_compute.graphs.charging_graph_global", 220)
    )

    assert coordinator.data["sessionStatus"] == "charging"
    assert coordinator.data["isPluggedIn"] is True
    assert coordinator.data["isCharging"] is True
    assert coordinator.data["power"] == 11.1
    assert coordinator.data["kilometersChargedPerHour"] == 61
    assert coordinator.data["timeElapsed"] == 60
    assert coordinator.data["totalChargedEnergy"] == 0
    assert coordinator.data["packChargedEnergy"] == 0
    assert coordinator.data["startTime"] == "1970-01-01T00:00:01.000000+0000"


def test_freshness_timeout_preserves_known_scheduled_not_charging(monkeypatch) -> None:
    """A timeout preserves the known false charging boolean for scheduled state."""
    coordinator = _coordinator()
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: SimpleNamespace(
            plug_connection_status_code=2,
            display_status_code=5,
            evse_type_code=1,
        ),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator._expire_transient_fields()

    assert coordinator.data["isPluggedIn"] is True
    assert coordinator.data["isCharging"] is False
    assert coordinator.data["sessionStatus"] == "unknown"


def test_active_startup_initializes_missing_session_values(monkeypatch) -> None:
    """A restart during charging exposes zeroes until live totals arrive."""
    coordinator = _coordinator()
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: SimpleNamespace(
            plug_connection_status_code=2,
            display_status_code=3,
            evse_type_code=1,
        ),
    )

    coordinator.process_status(_message("charging.session.status", 100))

    assert coordinator.data["sessionStatus"] == "charging"
    assert coordinator.data["isPluggedIn"] is True
    assert coordinator.data["isCharging"] is True
    assert all(
        coordinator.data[field] == 0 for field in coordinator._SESSION_NUMERIC_FIELDS
    )


def test_graph_uses_earliest_valid_start(monkeypatch) -> None:
    """The graph supplies a direct start time without deriving it from elapsed time."""
    coordinator = _coordinator()
    coordinator._plug_connection_status_code = 2
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_graph_global",
        lambda payload: SimpleNamespace(
            bars=(
                SimpleNamespace(start_time_ms=2_000),
                SimpleNamespace(start_time_ms=None),
                SimpleNamespace(start_time_ms=1_000),
            )
        ),
    )

    coordinator.process_graph(
        _message("energy_edge_compute.graphs.charging_graph_global", 200)
    )
    assert coordinator.data["startTime"] == "1970-01-01T00:00:01.000000+0000"


def test_freshness_expiry_only_removes_transient_fields() -> None:
    """A quiet stream expires even if the vehicle clock is ahead of the host."""
    coordinator = _coordinator()
    coordinator._plug_connection_status_code = 2
    coordinator._display_status_code = 3
    coordinator.data = {
        "power": 10.7,
        "kilometersChargedPerHour": 60,
        "timeRemaining": 300,
        "totalChargedEnergy": 2.2,
    }
    for field, value in coordinator.data.items():
        coordinator.observations.update(
            field, value, source="test", source_timestamp_ms=4_000_000_000_000
        )

    coordinator._expire_transient_fields()

    assert coordinator.data == {
        "isCharging": False,
        "sessionStatus": "unknown",
        "totalChargedEnergy": 2.2,
    }
    assert coordinator.observations.get("power") is None
    assert not coordinator.is_active


def test_delayed_status_does_not_regress_lifecycle(monkeypatch) -> None:
    """An older unplug frame cannot clear a newer active charging session."""
    coordinator = _coordinator()
    statuses = iter(
        (
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=3,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=1,
                display_status_code=1,
                evse_type_code=None,
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: next(statuses),
    )

    coordinator.process_status(_message("charging.session.status", 200))
    coordinator.process_status(_message("charging.session.status", 100))

    assert coordinator.data["isCharging"] is True
    assert coordinator.data["isPluggedIn"] is True
    assert coordinator.data["sessionStatus"] == "charging"


def test_old_live_replay_cannot_resurrect_expired_charging(monkeypatch) -> None:
    """A replayed breakdown cannot make timed-out charging active again."""
    coordinator = _coordinator()
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: SimpleNamespace(
            plug_connection_status_code=2,
            display_status_code=3,
            evse_type_code=1,
        ),
    )
    live_data = SimpleNamespace(
        total_kwh=1.0,
        pack_kwh=0.9,
        thermal_kwh=0.0,
        outlets_kwh=0.0,
        system_kwh=0.1,
        session_duration_minutes=5,
        time_remaining_minutes=60,
        range_added_km=5,
        current_power_kw=10.7,
        current_range_km_per_hour=60,
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: live_data,
    )

    coordinator.process_status(_message("charging.session.status", 200))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 210)
    )
    coordinator._expire_transient_fields()
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 190)
    )

    assert coordinator.data["isCharging"] is False
    assert coordinator.data["sessionStatus"] == "unknown"
    assert "power" not in coordinator.data
    assert not coordinator.is_active


def test_authoritative_unplug_resets_newer_breakdown_fields(monkeypatch) -> None:
    """Lifecycle reset wins over cross-topic timestamp skew after ordered unplug."""
    coordinator = _coordinator()
    statuses = iter(
        (
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=3,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=1,
                display_status_code=1,
                evse_type_code=None,
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: next(statuses),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=2.2,
            pack_kwh=2.1,
            thermal_kwh=0.0,
            outlets_kwh=0.0,
            system_kwh=0.1,
            session_duration_minutes=13,
            time_remaining_minutes=82,
            range_added_km=10,
            current_power_kw=10.7,
            current_range_km_per_hour=60,
        ),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 400)
    )
    coordinator.process_status(_message("charging.session.status", 300))

    assert coordinator.data["totalChargedEnergy"] == 0
    assert coordinator.data["power"] == 0
    assert coordinator.data["sessionStatus"] == "unplugged"
    assert (
        coordinator.observations.get_observation("totalChargedEnergy").source
        == "parallax:charging.session.status:lifecycle_reset"
    )


def test_unknown_status_clears_derived_booleans_without_resetting_live_data(
    monkeypatch,
) -> None:
    """Uncorrelated status codes retain evidence without inventing booleans."""
    charging = _coordinator()
    vehicle = _vehicle_coordinator()
    vehicle.charging_coordinator = charging
    vehicle.r2_charging_coordinator = charging
    charging.state_callback = vehicle._process_r2_charging_state
    statuses = iter(
        (
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=3,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=7,
                display_status_code=9,
                evse_type_code=4,
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: next(statuses),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=2.2,
            pack_kwh=2.1,
            thermal_kwh=0.0,
            outlets_kwh=0.0,
            system_kwh=0.1,
            session_duration_minutes=13,
            time_remaining_minutes=82,
            range_added_km=10,
            current_power_kw=10.7,
            current_range_km_per_hour=60,
        ),
    )

    charging.process_status(_message("charging.session.status", 100))
    charging.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 110)
    )
    freshness_timer = charging.hass.loop.timer
    charging.process_status(_message("charging.session.status", 120))

    assert charging.data["sessionStatus"] == "unknown"
    assert charging.data["totalChargedEnergy"] == 2.2
    assert "power" not in charging.data
    assert not charging.observations.get_observation("power").presence
    assert "isPluggedIn" not in charging.data
    assert "isCharging" not in charging.data
    assert not charging.observations.get_observation("isPluggedIn").presence
    assert not charging.observations.get_observation("isCharging").presence
    assert not vehicle.observations.get_observation("isPluggedIn").presence
    assert not vehicle.observations.get_observation("isCharging").presence
    assert freshness_timer.cancelled
    assert charging._freshness_timer is None
    diagnostics = charging.diagnostics()
    assert diagnostics["plug_connection_status_code"] == 7
    assert diagnostics["display_status_code"] == 9
    assert diagnostics["evse_type_code"] == 4


def test_frame_markers_keep_diagnostics_json_safe(monkeypatch) -> None:
    """Decoded client dataclasses and payload bytes are not retained in diagnostics."""
    coordinator = _coordinator()
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: SimpleNamespace(
            plug_connection_status_code=2,
            display_status_code=3,
            evse_type_code=4,
        ),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=2.2,
            pack_kwh=2.1,
            thermal_kwh=0.0,
            outlets_kwh=0.0,
            system_kwh=0.1,
            session_duration_minutes=13,
            time_remaining_minutes=82,
            range_added_km=10,
            current_power_kw=10.7,
            current_range_km_per_hour=60,
        ),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_time_estimation",
        lambda payload: SimpleNamespace(estimated_minutes_remaining=80),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_graph_global",
        lambda payload: SimpleNamespace(bars=(SimpleNamespace(start_time_ms=1_000),)),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 110)
    )
    coordinator.process_time_estimation(
        _message("charging.session.time_estimation", 120)
    )
    coordinator.process_graph(
        _message("energy_edge_compute.graphs.charging_graph_global", 130)
    )

    serialized = json.dumps(coordinator.diagnostics())
    assert '"evse_type_code": 4' in serialized
    assert "test" not in serialized


def test_late_session_topics_cannot_repopulate_after_unplug(monkeypatch) -> None:
    """Newer live, time, and graph frames cannot revive a known unplugged session."""
    coordinator = _coordinator()
    statuses = iter(
        (
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=3,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=1,
                display_status_code=1,
                evse_type_code=None,
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: next(statuses),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=9.0,
            pack_kwh=8.0,
            thermal_kwh=0.2,
            outlets_kwh=0.0,
            system_kwh=0.8,
            session_duration_minutes=60,
            time_remaining_minutes=20,
            range_added_km=100,
            current_power_kw=11.0,
            current_range_km_per_hour=70,
        ),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_time_estimation",
        lambda payload: SimpleNamespace(estimated_minutes_remaining=20),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_graph_global",
        lambda payload: SimpleNamespace(bars=(SimpleNamespace(start_time_ms=1_000),)),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator.process_status(_message("charging.session.status", 200))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 300)
    )
    coordinator.process_time_estimation(
        _message("charging.session.time_estimation", 310)
    )
    coordinator.process_graph(
        _message("energy_edge_compute.graphs.charging_graph_global", 320)
    )

    assert coordinator.data["totalChargedEnergy"] == 0
    assert coordinator.data["timeRemaining"] == 0
    assert "startTime" not in coordinator.data


def test_stopped_status_authoritatively_zeros_newer_live_values(monkeypatch) -> None:
    """A stopped lifecycle frame wins over cross-topic source-clock skew."""
    coordinator = _coordinator()
    statuses = iter(
        (
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=3,
                evse_type_code=1,
            ),
            SimpleNamespace(
                plug_connection_status_code=2,
                display_status_code=8,
                evse_type_code=1,
            ),
        )
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: next(statuses),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=2.2,
            pack_kwh=2.1,
            thermal_kwh=0.0,
            outlets_kwh=0.0,
            system_kwh=0.1,
            session_duration_minutes=13,
            time_remaining_minutes=82,
            range_added_km=10,
            current_power_kw=10.7,
            current_range_km_per_hour=60,
        ),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 400)
    )
    coordinator.process_status(_message("charging.session.status", 300))

    assert coordinator.data["power"] == 0
    assert coordinator.data["kilometersChargedPerHour"] == 0
    assert coordinator.observations.get_observation("power").source.endswith(
        ":lifecycle_reset"
    )


def test_freshness_timeout_allows_later_vehicle_timestamps(monkeypatch) -> None:
    """Local timeout metadata does not outrank later per-topic vehicle frames."""
    coordinator = _coordinator()
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_status",
        lambda payload: SimpleNamespace(
            plug_connection_status_code=2,
            display_status_code=3,
            evse_type_code=1,
        ),
    )
    monkeypatch.setattr(
        "custom_components.rivian.r2_coordinator.decode_charging_session_live_data",
        lambda payload: SimpleNamespace(
            total_kwh=2.2,
            pack_kwh=2.1,
            thermal_kwh=0.0,
            outlets_kwh=0.0,
            system_kwh=0.1,
            session_duration_minutes=13,
            time_remaining_minutes=82,
            range_added_km=10,
            current_power_kw=10.7,
            current_range_km_per_hour=60,
        ),
    )

    coordinator.process_status(_message("charging.session.status", 100))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 110)
    )
    coordinator._expire_transient_fields()
    coordinator.process_status(_message("charging.session.status", 120))
    coordinator.process_live_data(
        _message("energy_edge_compute.graphs.charge_session_breakdown", 130)
    )

    assert coordinator.data["isCharging"] is True
    assert coordinator.data["power"] == 10.7
    assert coordinator.observations.get_observation("power").source_timestamp_ms == 130


def test_r2_legacy_invalid_values_never_replace_durable_state() -> None:
    """Sentinel strings and generic nulls are absence, not R2 entity values."""
    coordinator = _vehicle_coordinator()

    for index, invalid_value in enumerate(
        ("undefined", "signal_not_available", "fault", None), start=1
    ):
        field = f"firstInvalid{index}"
        coordinator._process_new_data(
            {
                "payload": {
                    "data": {
                        "vehicleState": {
                            field: {
                                "value": invalid_value,
                                "timeStamp": "2026-01-01T00:00:01Z",
                            }
                        }
                    }
                }
            }
        )
        assert field not in coordinator.data
        assert coordinator.observations.get_observation(field) is None

    coordinator._process_new_data(
        {
            "payload": {
                "data": {
                    "vehicleState": {
                        "batteryLevel": {
                            "value": 80,
                            "timeStamp": "2026-01-01T00:00:02Z",
                        }
                    }
                }
            }
        }
    )
    for index, invalid_value in enumerate(
        ("undefined", "signal_not_available", "fault", None), start=3
    ):
        coordinator._process_new_data(
            {
                "payload": {
                    "data": {
                        "vehicleState": {
                            "batteryLevel": {
                                "value": invalid_value,
                                "timeStamp": f"2026-01-01T00:00:0{index}Z",
                            }
                        }
                    }
                }
            }
        )

    assert coordinator.get("batteryLevel") == 80
    assert coordinator.data["batteryLevel"]["value"] == 80
    assert (
        coordinator.observations.get_observation("batteryLevel").source_timestamp_ms
        == 1_767_225_602_000
    )


def test_r2_legacy_enum_aliases_match_parallax_entity_options() -> None:
    """Initial legacy enums use the same labels as later Parallax frames."""
    coordinator = _vehicle_coordinator()

    coordinator._process_new_data(
        {
            "payload": {
                "data": {
                    "vehicleState": {
                        "driveMode": {
                            "value": "everyday",
                            "timeStamp": "2026-01-01T00:00:01Z",
                        },
                        "gearStatus": {
                            "value": "park",
                            "timeStamp": "2026-01-01T00:00:01Z",
                        },
                    }
                }
            }
        }
    )

    assert coordinator.get("driveMode") == "All-Purpose"
    assert coordinator.get("gearStatus") == "Park"
    assert coordinator.data["driveMode"]["value"] == "All-Purpose"
    assert coordinator.data["gearStatus"]["value"] == "Park"


def test_closure_and_lock_derivations_require_correlated_codes() -> None:
    """Raw body codes persist while unknown derived states become unavailable."""
    coordinator = _vehicle_coordinator()
    closure_fields = {
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
    coordinator._apply_parallax_state(
        _message("body.closures.states", 100),
        SimpleNamespace(
            states=tuple(
                SimpleNamespace(position_code=position, state_code=1)
                for position in closure_fields
            )
        ),
    )
    for position, field in closure_fields.items():
        assert coordinator.get(field) == "open"
        assert coordinator.get(f"r2Closure{position}StateCode") == 1

    coordinator._apply_parallax_state(
        _message("body.closures.states", 200),
        SimpleNamespace(
            states=tuple(
                SimpleNamespace(position_code=position, state_code=2)
                for position in closure_fields
            )
        ),
    )
    for field in closure_fields.values():
        assert coordinator.get(field) == "closed"

    for code in (3, 4, 5):
        coordinator._apply_parallax_state(
            _message("body.closures.states", 200 + code),
            SimpleNamespace(
                states=(SimpleNamespace(position_code=1, state_code=code),)
            ),
        )
        assert coordinator.get("doorFrontLeftClosed") == "open"
        assert coordinator.get("r2Closure1StateCode") == code

    coordinator._apply_parallax_state(
        _message("body.closures.states", 300),
        SimpleNamespace(states=(SimpleNamespace(position_code=1, state_code=7),)),
    )
    assert coordinator.get("doorFrontLeftClosed") is None
    assert coordinator.get("r2Closure1StateCode") == 7

    coordinator._apply_parallax_state(
        _message("body.locks.states", 400),
        SimpleNamespace(
            states=tuple(
                SimpleNamespace(position_code=position, state_code=1)
                for position in (1, 2, 3, 4, 5, 7)
            )
        ),
    )
    assert coordinator.get("r2AllLocked") is True
    assert coordinator.get("r2Lock7StateCode") == 1

    coordinator._apply_parallax_state(
        _message("body.locks.states", 500),
        SimpleNamespace(
            states=tuple(
                SimpleNamespace(position_code=position, state_code=2)
                for position in (1, 2, 3, 4, 5, 7)
            )
        ),
    )
    assert coordinator.get("r2AllLocked") is False

    coordinator._apply_parallax_state(
        _message("body.locks.states", 525),
        SimpleNamespace(
            states=tuple(
                SimpleNamespace(
                    position_code=position,
                    state_code=3 if position == 2 else 1,
                )
                for position in (1, 2, 3, 4, 5, 7)
            )
        ),
    )
    assert coordinator.get("r2AllLocked") is False

    coordinator._apply_parallax_state(
        _message("body.locks.states", 550),
        SimpleNamespace(
            states=tuple(
                SimpleNamespace(
                    position_code=position,
                    state_code=2 if position == 2 else 1,
                )
                for position in (1, 2, 3, 4, 5, 7)
            )
        ),
    )
    assert coordinator.get("r2AllLocked") is False

    coordinator._apply_parallax_state(
        _message("body.locks.states", 600),
        SimpleNamespace(
            states=(
                SimpleNamespace(position_code=1, state_code=1),
                SimpleNamespace(position_code=2, state_code=9),
            )
        ),
    )
    assert coordinator.get("r2AllLocked") is None
    assert coordinator.get("r2Lock2StateCode") == 9


def test_gear_and_drive_mode_use_correlated_labels_with_raw_fallbacks() -> None:
    """R2 enums expose verified labels while retaining each raw integer."""
    coordinator = _vehicle_coordinator()

    for code, label in {1: "Park", 2: "Reverse", 3: "Neutral", 4: "Drive"}.items():
        coordinator._apply_parallax_state(
            _message("dynamics.vehicle.gear", code * 100),
            SimpleNamespace(state_code=code),
        )
        assert coordinator.get("gearStatus") == label
        assert coordinator.get("r2GearStateCode") == code

    for code, label in {
        2: "All-Purpose",
        4: "Rally",
        8: "Sport",
        9: "Conserve",
        11: "All-Terrain",
        12: "Soft Sand",
        15: "Snow",
    }.items():
        coordinator._apply_parallax_state(
            _message("dynamics.vehicle.drive_mode", code * 100),
            SimpleNamespace(mode_code=code),
        )
        assert coordinator.get("driveMode") == label
        assert coordinator.get("r2DriveModeCode") == code

    coordinator._apply_parallax_state(
        _message("dynamics.vehicle.gear", 10_000),
        SimpleNamespace(state_code=99),
    )
    coordinator._apply_parallax_state(
        _message("dynamics.vehicle.drive_mode", 10_000),
        SimpleNamespace(mode_code=99),
    )
    assert coordinator.get("gearStatus") == "unknown"
    assert coordinator.get("r2GearStateCode") == 99
    assert coordinator.get("driveMode") == "unknown"
    assert coordinator.get("r2DriveModeCode") == 99


def test_go_power_state_drives_existing_in_use_entity() -> None:
    """The correlated R2 driving power code uses the existing R1 semantic."""
    coordinator = _vehicle_coordinator()
    coordinator._apply_parallax_state(
        _message("vehicle.power.state", 100),
        SimpleNamespace(state_code=4),
    )

    assert coordinator.get("powerState") == "go"
    assert coordinator.get("r2PowerStateCode") == 4


def test_r1_and_r2_observation_access() -> None:
    """Shared accessors preserve R1 wrappers and expose R2 metadata."""
    r1 = object.__new__(VehicleCoordinator)
    r1.data = {
        "batteryLevel": {"value": 75, "timeStamp": "timestamp"},
        "gnssLocation": {"latitude": 1.0, "longitude": 2.0},
    }
    assert r1.get("batteryLevel") == 75
    assert r1.get_observation("batteryLevel")["timeStamp"] == "timestamp"
    assert r1.is_field_available("batteryLevel")
    assert r1.get_location() == {"latitude": 1.0, "longitude": 2.0}

    r2 = object.__new__(R2VehicleCoordinator)
    r2.data = {}
    r2.observations = R2ObservationStore()
    r2.observations.update(
        "batteryLevel", 80, source="parallax", source_timestamp_ms=10
    )
    assert r2.get("batteryLevel") == 80
    assert r2.get_observation("batteryLevel").source == "parallax"
    assert r2.is_field_available("batteryLevel")


def test_gnss_uses_nested_source_timestamp() -> None:
    """A newer envelope cannot replace an older cached GNSS sample."""
    coordinator = _vehicle_coordinator()
    coordinator._apply_parallax_state(
        _message("dynamics.vehicle.gnss", 200),
        SimpleNamespace(
            latitude=1.0,
            longitude=2.0,
            altitude_m=3.0,
            speed_m_s=20.0,
            heading_deg=-45.0,
            timestamp_ms=100,
        ),
    )
    coordinator._apply_parallax_state(
        _message("dynamics.vehicle.gnss", 300),
        SimpleNamespace(
            latitude=4.0,
            longitude=5.0,
            altitude_m=6.0,
            speed_m_s=30.0,
            heading_deg=90.0,
            timestamp_ms=90,
        ),
    )

    assert coordinator.get_location()["latitude"] == 1.0
    assert coordinator.get_observation("gnssLocation").source_timestamp_ms == 100
    assert coordinator.get("gnssSpeed") == 20.0
    assert coordinator.get("gnssBearing") == 315.0


def test_navigation_progress_drives_fresher_tracker_and_route_entities() -> None:
    """Five-second trip motion supersedes older GNSS and expires as one unit."""
    coordinator = _vehicle_coordinator()
    coordinator._apply_parallax_state(
        _message("dynamics.vehicle.gnss", 100),
        SimpleNamespace(
            latitude=1.0,
            longitude=2.0,
            altitude_m=3.0,
            speed_m_s=10.0,
            heading_deg=15.0,
            timestamp_ms=100,
        ),
    )
    coordinator._apply_parallax_state(
        _message("navigation.navigation_service.trip_info", 180),
        SimpleNamespace(
            trip_id="trip",
            destination_name="Home",
            destination_latitude=4.0,
            destination_longitude=5.0,
            eta_timestamp_ms=1_800_000,
        ),
    )
    coordinator._apply_parallax_state(
        _message("navigation.navigation_service.trip_progress", 200),
        SimpleNamespace(
            remaining_distance_m=12_345.0,
            remaining_drive_time_s=678.0,
            motion=SimpleNamespace(
                latitude=6.0,
                longitude=7.0,
                speed_m_s=25.0,
                heading_deg=-90.0,
                timestamp_ms=210,
            ),
        ),
    )
    coordinator._apply_parallax_state(
        _message("dynamics.vehicle.gnss", 300),
        SimpleNamespace(
            latitude=8.0,
            longitude=9.0,
            altitude_m=10.0,
            speed_m_s=5.0,
            heading_deg=45.0,
            timestamp_ms=150,
        ),
    )

    assert coordinator.get_location()["latitude"] == 6.0
    assert coordinator.get("gnssSpeed") == 25.0
    assert coordinator.get("gnssBearing") == 270.0
    assert coordinator.get("navigationDestination") == "Home"
    assert coordinator.get("navigationDistanceRemaining") == 12_345.0
    assert coordinator.get("navigationTimeRemaining") == 678.0
    assert coordinator.get("navigationEta").timestamp() == 1800

    timer = coordinator.hass.loop.timer
    assert timer is not None
    timer.callback()

    assert coordinator.get_location()["latitude"] == 6.0
    assert coordinator.get("gnssSpeed") is None
    assert coordinator.get("gnssBearing") is None
    assert coordinator.get("navigationDestination") is None
    assert coordinator.get("navigationDistanceRemaining") is None


def test_empty_trip_info_clears_route_but_preserves_non_navigation_motion() -> None:
    """A no-route snapshot clears trip entities without erasing ordinary GNSS."""
    coordinator = _vehicle_coordinator()
    coordinator._apply_parallax_state(
        _message("dynamics.vehicle.gnss", 100),
        SimpleNamespace(
            latitude=1.0,
            longitude=2.0,
            altitude_m=3.0,
            speed_m_s=4.0,
            heading_deg=5.0,
            timestamp_ms=100,
        ),
    )
    coordinator._apply_parallax_state(
        _message("navigation.navigation_service.trip_info", 200),
        SimpleNamespace(
            trip_id=None,
            destination_name=None,
            destination_latitude=None,
            destination_longitude=None,
            eta_timestamp_ms=None,
        ),
    )

    assert coordinator.get_location()["latitude"] == 1.0
    assert coordinator.get("gnssSpeed") == 4.0
    assert coordinator.get("gnssBearing") == 5.0


def test_tires_use_per_record_source_timestamp() -> None:
    """A replayed tire record keeps its durable newer pressure value."""
    coordinator = _vehicle_coordinator()
    coordinator._apply_parallax_state(
        _message("dynamics.tires.state", 200),
        SimpleNamespace(
            monitor_status_code=1,
            tires=(
                SimpleNamespace(
                    position_code=1,
                    status_code=2,
                    pressure_bar=2.5,
                    validity_code=1,
                    timestamp_ms=100,
                ),
            ),
        ),
    )
    coordinator._apply_parallax_state(
        _message("dynamics.tires.state", 300),
        SimpleNamespace(
            monitor_status_code=1,
            tires=(
                SimpleNamespace(
                    position_code=1,
                    status_code=2,
                    pressure_bar=2.0,
                    validity_code=1,
                    timestamp_ms=90,
                ),
            ),
        ),
    )

    assert coordinator.get("tirePressureFrontLeft") == 2.5
    assert (
        coordinator.get_observation("tirePressureFrontLeft").source_timestamp_ms == 100
    )


def test_preconditioning_status_codes_use_protocol_semantics() -> None:
    """All running phases are active while unsupported codes remain unavailable."""
    coordinator = _vehicle_coordinator()
    for code in (1, 2, 3, 4):
        coordinator._apply_parallax_state(
            _message("comfort.cabin.cabin_preconditioning_status", code * 100),
            SimpleNamespace(status_code=code, type_code=1),
        )
        assert coordinator.get("cabinPreconditioningStatus") == "active"

    coordinator._apply_parallax_state(
        _message("comfort.cabin.cabin_preconditioning_status", 500),
        SimpleNamespace(status_code=8, type_code=1),
    )
    assert coordinator.get("cabinPreconditioningStatus") is None
    assert coordinator.get_observation("cabinPreconditioningStatus").presence is False
    assert coordinator.get("r2CabinPreconditioningStatusCode") == 8

    coordinator._apply_parallax_state(
        _message("comfort.cabin.cabin_preconditioning_status", 600),
        SimpleNamespace(status_code=None, type_code=None),
    )
    assert coordinator.get("cabinPreconditioningStatus") == "inactive"


def test_battery_state_exposes_capacity_not_current_energy() -> None:
    """The fixed pack-size field is published as capacity, not stored energy."""
    coordinator = _vehicle_coordinator()
    coordinator._apply_parallax_state(
        _message("energy.high_voltage.battery_state", 100),
        SimpleNamespace(
            soc_percent=80.0,
            capacity_kwh=91.52,
            range_km=400.0,
            cell_average_c=25.0,
            cell_max_c=26.0,
            cell_min_c=24.0,
            power_output_code=1,
            requires_calibration=False,
            cold_weather_state_code=1,
        ),
    )

    assert coordinator.get("batteryCapacity") == 91.52
    assert coordinator.get("batteryEnergy") is None


def test_r2_startup_accepts_ack_without_a_frame() -> None:
    """An asleep R2 is healthy when both subscriptions return handles."""

    async def unsubscribe() -> None:
        pass

    api = SimpleNamespace(
        subscribe_for_parallax_messages=lambda **kwargs: _async_value(unsubscribe),
        subscribe_for_vehicle_updates=lambda **kwargs: _async_value(unsubscribe),
    )
    coordinator = object.__new__(R2VehicleCoordinator)
    coordinator.api = api
    coordinator.vehicle_id = "vehicle"
    coordinator.parallax_rvms = {"vehicle.power.state"}
    coordinator._parallax_unsubscribe = None
    coordinator._unsub_handler = None
    coordinator.data = {}

    assert asyncio.run(coordinator._async_update_data()) == {}
    assert coordinator._parallax_unsubscribe is unsubscribe
    assert coordinator._unsub_handler is unsubscribe


def test_r2_startup_rejects_missing_subscription_handle() -> None:
    """A connect or acknowledgement failure must not look like a sleeping R2."""

    async def missing_handle(**kwargs):
        return None

    coordinator = object.__new__(R2VehicleCoordinator)
    coordinator.api = SimpleNamespace(
        subscribe_for_parallax_messages=missing_handle,
    )
    coordinator.vehicle_id = "vehicle"
    coordinator.parallax_rvms = {"vehicle.power.state"}
    coordinator._parallax_unsubscribe = None
    coordinator._unsub_handler = None
    coordinator.data = {}

    try:
        asyncio.run(coordinator._async_update_data())
    except UpdateFailed:
        pass
    else:
        raise AssertionError("Missing subscription handle was accepted")


async def _async_value(value):
    """Return a value through a coroutine for a lightweight API test double."""
    return value
