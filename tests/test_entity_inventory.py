"""Entity inventory and model-routing regression tests."""

import asyncio
from importlib import import_module
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from custom_components.rivian import (
    _async_remove_obsolete_r2_entities,
    climate,
    cover,
    lock,
    number,
    select,
    switch,
)
from custom_components.rivian.binary_sensor import binary_sensor_descriptions
from custom_components.rivian.const import (
    ATTR_COORDINATOR,
    ATTR_USER,
    ATTR_VEHICLE,
    BINARY_SENSORS,
    DOMAIN,
    SENSORS,
)
from custom_components.rivian.r2 import supports_vehicle_control
from custom_components.rivian.sensor import (
    CHARGING_SENSORS,
    charging_sensor_descriptions,
    vehicle_sensor_descriptions,
)


def _keys(descriptions) -> tuple[str, ...]:
    return tuple(description.key for description in descriptions)


def test_r1_profiles_and_unique_ids_are_unchanged() -> None:
    """R1 model matching continues to use the complete legacy inventories."""
    vin = "vin"
    for model in ("R1", "R1S", "R1T"):
        vehicle = {"model": model}
        expected_sensors = tuple(
            description
            for model_key, descriptions in SENSORS.items()
            if model_key in model
            for description in descriptions
        )
        expected_binary = tuple(
            description
            for model_key, descriptions in BINARY_SENSORS.items()
            if model_key in model
            for description in descriptions
        )
        assert vehicle_sensor_descriptions(vehicle) == expected_sensors
        assert binary_sensor_descriptions(vehicle) == expected_binary
        assert charging_sensor_descriptions(vehicle) is CHARGING_SENSORS
        assert tuple(f"{vin}-{key}" for key in _keys(expected_sensors)) == tuple(
            f"{vin}-{description.key}" for description in expected_sensors
        )


def test_r2_inventory_is_stable_and_capability_gated() -> None:
    """R2 profiles are selected from capabilities rather than the first frame."""
    base_vehicle = {"model": "R2", "supported_features": []}
    assert set(_keys(vehicle_sensor_descriptions(base_vehicle))) == {
        "active_driver",
        "battery_capacity",
        "battery_level",
        "battery_limit",
        "distance_to_empty",
        "ota_current_version",
        "service_mode",
        "vehicle_mileage",
    }
    assert binary_sensor_descriptions(base_vehicle) == ()
    assert charging_sensor_descriptions(base_vehicle) == ()

    full_vehicle = {
        "model": "R2",
        "supported_features": ["PX_STATE_ALL", "CHARG_DATA_PX"],
    }
    assert set(_keys(binary_sensor_descriptions(full_vehicle))) == {
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
        "use_state",
    }
    assert set(_keys(charging_sensor_descriptions(full_vehicle))) == {
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
    r2_charging = {
        description.key: description
        for description in charging_sensor_descriptions(full_vehicle)
    }
    assert r2_charging["charging_speed"].name == "Charging Power"
    assert (
        next(
            description
            for description in CHARGING_SENSORS
            if description.key == "charging_speed"
        ).name
        == "Charging Speed"
    )
    assert {
        "altitude",
        "bearing",
        "battery_cell_average_temperature",
        "battery_cell_max_temperature",
        "battery_cell_min_temperature",
        "battery_capacity",
        "cabin_temperature",
        "drive_mode",
        "gear_status",
        "power_state",
        "speed",
        "tire_pressure_front_left",
        "tire_pressure_front_right",
        "tire_pressure_rear_left",
        "tire_pressure_rear_right",
    } <= set(_keys(vehicle_sensor_descriptions(full_vehicle)))
    r2_sensors = {
        description.key: description
        for description in vehicle_sensor_descriptions(full_vehicle)
    }
    assert "Unknown" in (r2_sensors["power_state"].options or ())
    r1_power_state = next(
        description for description in SENSORS["R1"] if description.key == "power_state"
    )
    assert "Unknown" not in (r1_power_state.options or ())

    navigation_vehicle = {
        "model": "R2",
        "supported_features": ["TRIP_NAV_PX"],
    }
    assert set(_keys(vehicle_sensor_descriptions(navigation_vehicle))) == {
        *set(_keys(vehicle_sensor_descriptions(base_vehicle))),
        "navigation_destination",
        "navigation_distance_remaining",
        "navigation_eta",
        "navigation_time_remaining",
    }


def test_registry_cleanup_only_removes_obsolete_exact_r2_entities() -> None:
    """Cleanup preserves R1 and supported R2 registry entries and history."""
    registry = SimpleNamespace(async_remove=lambda entity_id: removed.append(entity_id))
    removed = []
    entries = [
        SimpleNamespace(
            entity_id="sensor.r2_charging_cost", unique_id="r2-vin-charging_cost"
        ),
        SimpleNamespace(
            entity_id="cover.r2_charge_port", unique_id="r2-vin-charge_port"
        ),
        SimpleNamespace(entity_id="cover.r2_frunk", unique_id="r2-vin-frunk"),
        SimpleNamespace(
            entity_id="sensor.r2_battery_level", unique_id="r2-vin-battery_level"
        ),
        SimpleNamespace(
            entity_id="sensor.r2_battery_energy", unique_id="r2-vin-battery_energy"
        ),
        SimpleNamespace(
            entity_id="sensor.r1_charging_cost", unique_id="r1-vin-charging_cost"
        ),
    ]
    with (
        patch("custom_components.rivian.er.async_get", return_value=registry),
        patch(
            "custom_components.rivian.er.async_entries_for_config_entry",
            return_value=entries,
        ),
    ):
        _async_remove_obsolete_r2_entities(
            SimpleNamespace(),
            SimpleNamespace(entry_id="entry"),
            {
                "r2": {"model": "R2", "vin": "r2-vin"},
                "r1": {"model": "R1S", "vin": "r1-vin"},
            },
        )

    assert removed == [
        "sensor.r2_charging_cost",
        "cover.r2_charge_port",
        "cover.r2_frunk",
        "sensor.r2_battery_energy",
    ]


def test_every_control_platform_emits_no_r2_entities() -> None:
    """An old saved phone identity cannot enable the R1 command path on R2."""
    vehicle = {
        "id": "vehicle",
        "vin": "vin",
        "model": "R2",
        "name": "R2",
        "phone_identity_id": "legacy-phone",
        "supported_features": ["PX_STATE_ALL", "CHARG_DATA_PX"],
    }
    assert not supports_vehicle_control(vehicle)

    async def run_setup_checks() -> None:
        bluetooth = ModuleType("homeassistant.components.bluetooth")
        bluetooth.BluetoothScanningMode = SimpleNamespace(ACTIVE="active")
        with patch.dict(sys.modules, {"homeassistant.components.bluetooth": bluetooth}):
            button = import_module("custom_components.rivian.button")
        entry = SimpleNamespace(entry_id="entry")
        hass = SimpleNamespace(
            data={
                DOMAIN: {
                    entry.entry_id: {
                        ATTR_VEHICLE: {vehicle["id"]: vehicle},
                        ATTR_COORDINATOR: {
                            ATTR_VEHICLE: {vehicle["id"]: object()},
                            ATTR_USER: object(),
                        },
                    }
                }
            }
        )
        for platform in (button, climate, cover, lock, number, select, switch):
            added = []
            await platform.async_setup_entry(hass, entry, added.extend)
            assert added == [], platform.__name__

    asyncio.run(run_setup_checks())
