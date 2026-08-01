"""Tests for the host-neutral capability model."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from custom_components.domoticz_sync.core import (
    Availability,
    Capability,
    CapabilityKind,
    CompoundCapability,
    SourceIdentity,
)


@pytest.fixture
def source() -> SourceIdentity:
    """Return a representative source identity."""
    return SourceIdentity(
        system="home_assistant",
        instance_id="instance-1",
        object_id="entity-registry-id",
        capability_id="state",
    )


def test_source_identity_has_stable_tuple_key(source: SourceIdentity) -> None:
    """Source identity is stable, hashable, and delimiter independent."""
    assert source.key == (
        "home_assistant",
        "instance-1",
        "entity-registry-id",
        "state",
    )
    assert {source: "mapped"}[source] == "mapped"


@pytest.mark.parametrize(
    "field_name",
    ("system", "instance_id", "object_id", "capability_id"),
)
def test_source_identity_rejects_empty_components(field_name: str) -> None:
    """Every identity component must contribute to uniqueness."""
    values = {
        "system": "domoticz",
        "instance_id": "entry-1",
        "object_id": "42",
        "capability_id": "temperature",
    }
    values[field_name] = " "

    with pytest.raises(ValueError, match=field_name):
        SourceIdentity(**values)


def test_source_identity_rejects_surrounding_whitespace() -> None:
    """Invisible differences cannot create duplicate source identities."""
    with pytest.raises(ValueError, match="surrounding whitespace"):
        SourceIdentity(
            system="domoticz",
            instance_id=" entry-1",
            object_id="42",
            capability_id="temperature",
        )


@pytest.mark.parametrize(
    ("kind", "value"),
    (
        (CapabilityKind.NUMERIC, 21.5),
        (CapabilityKind.NUMERIC, 21),
        (CapabilityKind.BINARY, True),
        (CapabilityKind.TEXT, "Tomorrow: paper"),
    ),
)
def test_available_capability_preserves_typed_value(
    source: SourceIdentity,
    kind: CapabilityKind,
    value: bool | int | float | str,
) -> None:
    """Supported available values retain their exact scalar type."""
    capability = Capability(source, kind, "State", value)

    assert capability.value == value
    assert type(capability.value) is type(value)
    assert capability.is_available


@pytest.mark.parametrize(
    "availability",
    (Availability.UNKNOWN, Availability.UNAVAILABLE),
)
def test_non_available_capability_has_no_value(
    source: SourceIdentity,
    availability: Availability,
) -> None:
    """Unknown and unavailable do not silently become zero or off."""
    capability = Capability(
        source,
        CapabilityKind.NUMERIC,
        "Temperature",
        None,
        availability,
        semantic="temperature",
        unit="celsius",
        state_class="measurement",
    )

    assert capability.value is None
    assert capability.state_class == "measurement"
    assert not capability.is_available


@pytest.mark.parametrize(
    ("kind", "value", "message"),
    (
        (CapabilityKind.NUMERIC, True, "int or float"),
        (CapabilityKind.NUMERIC, "21.5", "int or float"),
        (CapabilityKind.BINARY, 1, "bool"),
        (CapabilityKind.TEXT, 21, "string"),
    ),
)
def test_capability_rejects_wrong_value_shape(
    source: SourceIdentity,
    kind: CapabilityKind,
    value: bool | int | str,
    message: str,
) -> None:
    """Value shape must match the declared capability kind."""
    with pytest.raises(TypeError, match=message):
        Capability(source, kind, "State", value)


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_numeric_capability_rejects_non_finite_value(
    source: SourceIdentity,
    value: float,
) -> None:
    """Numeric values remain safe for JSON and MQTT transports."""
    with pytest.raises(ValueError, match="finite"):
        Capability(source, CapabilityKind.NUMERIC, "Temperature", value)


def test_non_available_capability_rejects_stale_value(
    source: SourceIdentity,
) -> None:
    """Adapters cannot accidentally publish stale values as current."""
    with pytest.raises(ValueError, match="must have no value"):
        Capability(
            source,
            CapabilityKind.BINARY,
            "Motion",
            False,
            Availability.UNAVAILABLE,
        )


def test_unit_is_numeric_only(source: SourceIdentity) -> None:
    """Units cannot be attached to binary or text values."""
    with pytest.raises(ValueError, match="only numeric"):
        Capability(
            source,
            CapabilityKind.TEXT,
            "Status",
            "ok",
            unit="celsius",
        )


def test_numeric_capability_preserves_sensor_state_class(
    source: SourceIdentity,
) -> None:
    """Numeric sensor metadata participates in immutable snapshot equality."""
    measurement = Capability(
        source,
        CapabilityKind.NUMERIC,
        "Temperature",
        21.5,
        state_class="measurement",
    )

    assert measurement.state_class == "measurement"
    assert measurement == Capability(
        source,
        CapabilityKind.NUMERIC,
        "Temperature",
        21.5,
        state_class="measurement",
    )
    assert measurement != Capability(
        source,
        CapabilityKind.NUMERIC,
        "Temperature",
        21.5,
        state_class="total",
    )


@pytest.mark.parametrize("state_class", ("", " ", " measurement", "total ", 123))
def test_capability_rejects_invalid_sensor_state_class(
    source: SourceIdentity,
    state_class: object,
) -> None:
    """State class metadata is either a meaningful string or absent."""
    with pytest.raises((TypeError, ValueError), match="state_class"):
        Capability(
            source,
            CapabilityKind.NUMERIC,
            "Temperature",
            21.5,
            state_class=state_class,
        )


def test_state_class_is_numeric_only(source: SourceIdentity) -> None:
    """Only numeric sensor capabilities can carry state class metadata."""
    with pytest.raises(ValueError, match="only numeric"):
        Capability(
            source,
            CapabilityKind.BINARY,
            "Motion",
            True,
            state_class="measurement",
        )


def test_capability_records_are_immutable(source: SourceIdentity) -> None:
    """Identity and state snapshots cannot change underneath an adapter."""
    capability = Capability(source, CapabilityKind.BINARY, "Motion", True)

    with pytest.raises(FrozenInstanceError):
        capability.value = False


def test_compound_capability_creation_and_attributes(source: SourceIdentity) -> None:
    """A compound preserves its nested capabilities."""
    cap1 = Capability(source, CapabilityKind.NUMERIC, "Temperature", 21.5)
    cap2 = Capability(
        SourceIdentity(
            "home_assistant", "instance-1", "entity-registry-id", "humidity"
        ),
        CapabilityKind.NUMERIC,
        "Humidity",
        50.0,
    )

    compound = CompoundCapability(
        source=SourceIdentity(
            "home_assistant", "instance-1", "entity-registry-id", "temp_hum"
        ),
        name="Climate Sensor",
        capabilities=(cap1, cap2),
    )

    assert compound.kind is CapabilityKind.COMPOUND
    assert compound.name == "Climate Sensor"
    assert compound.capabilities == (cap1, cap2)
    assert compound.value is None
    assert compound.semantic is None
    assert compound.unit is None
    assert compound.state_class is None
    assert compound.is_available
    # Nested capabilities must retain their exact source identities (not lost)
    assert compound.capabilities[0].source.capability_id == "state"
    assert compound.capabilities[1].source.capability_id == "humidity"


def test_compound_capability_validation() -> None:
    """CompoundCapability validates its fields strictly on initialization."""
    with pytest.raises(TypeError, match="source"):
        CompoundCapability(source="not-a-source", name="Invalid", capabilities=())

    with pytest.raises(TypeError, match="name"):
        CompoundCapability(
            source=SourceIdentity("a", "b", "c", "d"), name=123, capabilities=()
        )

    with pytest.raises(ValueError, match="name"):
        CompoundCapability(
            source=SourceIdentity("a", "b", "c", "d"), name="   ", capabilities=()
        )

    with pytest.raises(TypeError, match="capabilities"):
        CompoundCapability(
            source=SourceIdentity("a", "b", "c", "d"),
            name="Valid",
            capabilities="not-a-tuple",
        )

    with pytest.raises(TypeError, match="capabilities"):
        CompoundCapability(
            source=SourceIdentity("a", "b", "c", "d"),
            name="Valid",
            capabilities=(123,),
        )
