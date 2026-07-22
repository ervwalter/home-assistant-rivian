"""Tests for exact-R2 profile and observation semantics."""

from datetime import datetime, timezone

from custom_components.rivian.r2 import (
    ObservationValidity,
    R2ObservationStore,
    is_r2_vehicle,
    r2_parallax_rvms,
)


def test_exact_r2_model_detection() -> None:
    """Only the exact R2 API model selects the R2 path."""
    assert is_r2_vehicle({"model": "R2"})
    assert not is_r2_vehicle({"model": "R1S"})
    assert not is_r2_vehicle({"model": "R2 Adventure"})


def test_parallax_topics_are_feature_gated() -> None:
    """Do not subscribe to a domain the vehicle does not advertise."""
    charging_only = r2_parallax_rvms(
        {"model": "R2", "supported_features": ["CHARG_DATA_PX"]}
    )
    assert "charging.session.status" in charging_only
    assert "energy.high_voltage.battery_state" not in charging_only

    state_only = r2_parallax_rvms(
        {"model": "R2", "supported_features": ["PX_STATE_ALL"]}
    )
    assert "energy.high_voltage.battery_state" in state_only
    assert "body.closures.states" in state_only
    assert "body.windows.states" not in state_only
    assert "charging.session.status" not in state_only

    navigation = r2_parallax_rvms(
        {"model": "R2", "supported_features": ["ACTIVE_TRIP"]}
    )
    assert navigation == {
        "navigation.navigation_service.trip_info",
        "navigation.navigation_service.trip_progress",
    }
    assert navigation == r2_parallax_rvms(
        {"model": "R2", "supported_features": ["TRIP_NAV_PX"]}
    )


def test_observations_preserve_explicit_zero_and_false() -> None:
    """Explicit proto defaults must not be confused with absent fields."""
    store = R2ObservationStore()
    assert store.update("power", 0, source="test", source_timestamp_ms=10)
    assert store.update("plugged", False, source="test", source_timestamp_ms=10)

    assert store.get("power") == 0
    assert store.get("plugged") is False
    assert store.diagnostics()["plugged"]["presence"] is True


def test_older_updates_and_clears_are_rejected() -> None:
    """Delayed frames must not replace newer per-field observations."""
    store = R2ObservationStore()
    received_at = datetime(2026, 7, 19, tzinfo=timezone.utc)
    assert store.update(
        "battery_level",
        80,
        source="new",
        source_timestamp_ms=200,
        received_at=received_at,
    )

    assert not store.update("battery_level", 70, source="old", source_timestamp_ms=100)
    assert not store.clear("battery_level", source="old", source_timestamp_ms=100)
    assert store.get("battery_level") == 80


def test_clear_records_absence_and_reason() -> None:
    """An unplug clear remains distinguishable from a never-observed value."""
    store = R2ObservationStore()
    assert store.update("power", 10.7, source="charging", source_timestamp_ms=10)
    assert store.clear("power", source="unplug", source_timestamp_ms=20)

    assert store.get("power") is None
    diagnostics = store.diagnostics()["power"]
    assert diagnostics["presence"] is False
    assert diagnostics["validity"] == ObservationValidity.CLEARED
    assert diagnostics["source"] == "unplug"


def test_unknown_order_does_not_replace_timestamped_data() -> None:
    """A timestamp-less frame cannot supersede an ordered observation."""
    store = R2ObservationStore()
    assert store.update("range", 100, source="known", source_timestamp_ms=10)
    assert not store.update("range", 90, source="unknown")
    assert store.get("range") == 100


def test_timestamp_less_data_uses_receive_order() -> None:
    """Receive time orders fields when neither source provides a timestamp."""
    store = R2ObservationStore()
    later = datetime(2026, 7, 19, 12, 1, tzinfo=timezone.utc)
    earlier = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    assert store.update("state", "new", source="test", received_at=later)
    assert not store.update("state", "old", source="test", received_at=earlier)
    assert store.get("state") == "new"


def test_equal_source_timestamp_is_idempotent() -> None:
    """Reconnect snapshots do not churn receive time at the same source time."""
    store = R2ObservationStore()
    first_received = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    assert store.update(
        "soc",
        80,
        source="test",
        source_timestamp_ms=10,
        received_at=first_received,
    )
    assert not store.update(
        "soc",
        80,
        source="test",
        source_timestamp_ms=10,
        received_at=datetime(2026, 7, 19, 12, 1, tzinfo=timezone.utc),
    )
    assert store.get_observation("soc").received_at == first_received
