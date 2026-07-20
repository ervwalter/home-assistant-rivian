"""Legacy R1 API routing regression tests."""

import asyncio
from types import SimpleNamespace

from custom_components.rivian.const import CHARGING_API_FIELDS, VEHICLE_STATE_API_FIELDS
from custom_components.rivian.coordinator import ChargingCoordinator, VehicleCoordinator


def test_legacy_vehicle_uses_only_vehicle_state_subscription() -> None:
    """The R1 coordinator never enters the R2 Parallax subscription path."""
    calls = []

    async def unsubscribe() -> None:
        pass

    async def subscribe_for_vehicle_updates(**kwargs):
        calls.append(kwargs)
        return unsubscribe

    async def subscribe_for_parallax_messages(**kwargs):
        raise AssertionError("Legacy coordinator requested Parallax telemetry")

    coordinator = object.__new__(VehicleCoordinator)
    coordinator.api = SimpleNamespace(
        subscribe_for_vehicle_updates=subscribe_for_vehicle_updates,
        subscribe_for_parallax_messages=subscribe_for_parallax_messages,
    )
    coordinator.vehicle_id = "vehicle"
    coordinator.data = {}
    coordinator._last_update_success = True
    coordinator._unsub_handler = None
    coordinator._initial = asyncio.Event()
    coordinator._initial.set()

    assert asyncio.run(coordinator._async_update_data()) == {}
    assert len(calls) == 1
    assert calls[0]["vehicle_id"] == "vehicle"
    assert calls[0]["properties"] is VEHICLE_STATE_API_FIELDS


def test_legacy_charging_polling_contract() -> None:
    """R1 charging retains its query fields and plugged/unplugged cadence."""
    queries = []

    async def get_live_charging_session(**kwargs):
        queries.append(kwargs)
        return "response"

    coordinator = object.__new__(ChargingCoordinator)
    coordinator.api = SimpleNamespace(
        get_live_charging_session=get_live_charging_session
    )
    coordinator.vehicle_id = "vehicle"
    intervals = []
    coordinator._set_update_interval = intervals.append

    assert asyncio.run(coordinator._fetch_data()) == "response"
    assert queries == [{"vin": "vehicle", "properties": CHARGING_API_FIELDS}]
    coordinator.adjust_update_interval(is_plugged_in=True)
    coordinator.adjust_update_interval(is_plugged_in=False)
    assert intervals == [30, 15 * 60]
