"""Binary sensor value interpretation tests."""

from types import SimpleNamespace

from custom_components.rivian.binary_sensor import RivianBinarySensorEntity
from custom_components.rivian.data_classes import RivianBinarySensorEntityDescription


def _entity(value, *, on_value, negate: bool = False) -> RivianBinarySensorEntity:
    """Build an entity around the observation accessor without HA setup."""
    entity = object.__new__(RivianBinarySensorEntity)
    entity._observation_coordinator = SimpleNamespace(get=lambda key: value)
    entity.entity_description = RivianBinarySensorEntityDescription(
        key="test",
        field="field",
        on_value=on_value,
        negate=negate,
    )
    return entity


def test_scalar_boolean_on_values() -> None:
    """Boolean descriptions compare as scalars instead of iterable containers."""
    assert _entity(True, on_value=True).is_on is True
    assert _entity(False, on_value=True).is_on is False
    assert _entity(False, on_value=False).is_on is True


def test_string_list_and_negated_semantics_are_preserved() -> None:
    """Established string, list, and negate behavior remains unchanged."""
    assert _entity("active", on_value="active").is_on is True
    assert _entity("ready", on_value=["active", "ready"]).is_on is True
    assert _entity("disconnected", on_value="disconnected", negate=True).is_on is False
