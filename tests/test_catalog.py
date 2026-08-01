"""Tests for the versioned, transport-neutral target catalog."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from custom_components.domoticz_sync.core.capabilities import (
    Availability,
    Capability,
    CapabilityKind,
    CompoundCapability,
    SourceIdentity,
)
from custom_components.domoticz_sync.core.catalog import (
    CATALOG_SCHEMA_VERSION,
    CatalogFormatError,
    TargetCatalog,
    catalog_from_document,
    catalog_to_document,
)
from custom_components.domoticz_sync.core.reconciliation import TargetRecord


def _source(object_id: str) -> SourceIdentity:
    return SourceIdentity(
        system="home_assistant",
        instance_id="instance-1",
        object_id=object_id,
        capability_id="state",
    )


def _capability(
    object_id: str,
    *,
    kind: CapabilityKind = CapabilityKind.NUMERIC,
    value: object = 21.5,
    availability: Availability = Availability.AVAILABLE,
    name: str = "Temperature",
    semantic: str | None = "temperature",
    unit: str | None = "celsius",
    state_class: str | None = "measurement",
) -> Capability:
    return Capability(
        source=_source(object_id),
        kind=kind,
        name=name,
        value=value,
        availability=availability,
        semantic=semantic,
        unit=unit,
        state_class=state_class,
    )


def _record(
    object_id: str,
    target_id: str | None = None,
    *,
    capability: Capability | None = None,
    stale: bool = False,
    pending: bool = False,
) -> TargetRecord:
    return TargetRecord(
        target_id=target_id or "target-" + object_id,
        capability=capability or _capability(object_id),
        stale=stale,
        pending=pending,
    )


def _serialized_record(object_id: str = "sensor-1") -> dict[str, Any]:
    return TargetCatalog([_record(object_id)]).to_dict()["targets"][0]


def test_empty_catalog_is_immutable_and_iterable() -> None:
    """The catalog exposes an immutable tuple rather than mutable storage."""
    catalog = TargetCatalog()

    assert catalog.records == ()
    assert tuple(catalog) == ()
    assert len(catalog) == 0
    with pytest.raises(FrozenInstanceError):
        catalog._records = (_record("new"),)


def test_records_are_sorted_by_complete_source_identity_key() -> None:
    """Input order does not affect persisted or execution ordering."""
    source_a = SourceIdentity("a", "z", "z", "z")
    source_b = SourceIdentity("b", "a", "a", "a")
    record_b = TargetRecord(
        "target-b",
        Capability(source_b, CapabilityKind.BINARY, "B", True),
    )
    record_a = TargetRecord(
        "target-a",
        Capability(source_a, CapabilityKind.BINARY, "A", False),
    )

    catalog = TargetCatalog([record_b, record_a])

    assert catalog.records == (record_a, record_b)


def test_lookup_uses_complete_source_identity() -> None:
    """Lookup neither guesses from display names nor partial identifiers."""
    record = _record("sensor-1")
    catalog = TargetCatalog([record])

    assert catalog.get(record.capability.source) is record
    assert catalog.get(_source("missing")) is None
    with pytest.raises(TypeError, match="SourceIdentity"):
        catalog.get("sensor-1")


def test_catalog_rejects_non_record_values() -> None:
    """Only validated target records can enter the catalog."""
    with pytest.raises(TypeError, match="TargetRecord"):
        TargetCatalog([object()])


def test_catalog_rejects_duplicate_source_identity() -> None:
    """One source capability cannot map to two targets."""
    capability = _capability("same")

    with pytest.raises(ValueError, match="duplicate source identity"):
        TargetCatalog(
            [
                TargetRecord("target-a", capability),
                TargetRecord("target-b", capability),
            ]
        )


def test_catalog_rejects_duplicate_target_id_globally() -> None:
    """A target ID cannot be reused across source systems or instances."""
    outside_source = SourceIdentity("domoticz", "other", "device-2", "state")
    outside = Capability(
        outside_source,
        CapabilityKind.BINARY,
        "Outside",
        True,
    )

    with pytest.raises(ValueError, match="duplicate target_id"):
        TargetCatalog(
            [
                _record("sensor-1", "same-target"),
                TargetRecord("same-target", outside),
            ]
        )


def test_upsert_inserts_without_mutating_original_catalog() -> None:
    """Adding a mapping returns a new deterministically ordered value."""
    original = TargetCatalog([_record("sensor-b")])
    added = _record("sensor-a")

    updated = original.with_record(added)

    assert [record.capability.source.object_id for record in updated] == [
        "sensor-a",
        "sensor-b",
    ]
    assert original.get(added.capability.source) is None


def test_upsert_replaces_the_same_source_mapping() -> None:
    """Confirmed target state replaces only its prior source record."""
    original = _record("sensor-1", "target-1")
    replacement = _record(
        "sensor-1",
        "target-1",
        capability=_capability("sensor-1", value=22.0),
    )

    updated = TargetCatalog([original]).with_record(replacement)

    assert updated.records == (replacement,)


def test_upsert_rejects_rebinding_source_to_a_different_target() -> None:
    """A confirmed source mapping cannot silently switch target identity."""
    catalog = TargetCatalog([_record("sensor-1", "old-target")])

    with pytest.raises(ValueError, match="already bound"):
        catalog.with_record(_record("sensor-1", "new-target"))


def test_upsert_rejects_target_id_owned_by_another_source() -> None:
    """An insert cannot silently steal an existing target mapping."""
    catalog = TargetCatalog([_record("sensor-1", "target-1")])

    with pytest.raises(ValueError, match="duplicate target_id"):
        catalog.with_record(_record("sensor-2", "target-1"))


def test_catalog_deliberately_has_no_removal_api() -> None:
    """Step 5 cannot accidentally introduce automatic deletion."""
    assert not hasattr(TargetCatalog, "remove")
    assert not hasattr(TargetCatalog, "discard")


def test_v3_serialization_contains_the_complete_record() -> None:
    """All identity, value, metadata, target, and lifecycle fields persist."""
    record = _record("sensor-1")

    assert catalog_to_document(TargetCatalog([record])) == {
        "version": 3,
        "targets": [
            {
                "target_id": "target-sensor-1",
                "capability": {
                    "source": {
                        "system": "home_assistant",
                        "instance_id": "instance-1",
                        "object_id": "sensor-1",
                        "capability_id": "state",
                    },
                    "kind": "numeric",
                    "name": "Temperature",
                    "value": 21.5,
                    "availability": "available",
                    "semantic": "temperature",
                    "unit": "celsius",
                    "state_class": "measurement",
                },
                "stale": False,
                "pending": False,
            }
        ],
    }


def test_v3_serialization_preserves_absent_state_class_as_null() -> None:
    """The strict schema retains an explicit nullable metadata field."""
    capability = _capability("sensor-1", state_class=None)
    catalog = TargetCatalog([_record("sensor-1", capability=capability)])

    document = catalog_to_document(catalog)

    assert document["targets"][0]["capability"]["state_class"] is None
    assert catalog_from_document(document) == catalog


def test_all_capability_shapes_round_trip_through_json() -> None:
    """Schema v3 preserves each value kind and availability state exactly."""
    stale_capability = _capability(
        "stale",
        value=None,
        availability=Availability.UNAVAILABLE,
    )
    records = [
        _record("numeric"),
        _record(
            "binary",
            capability=_capability(
                "binary",
                kind=CapabilityKind.BINARY,
                value=True,
                semantic="opening",
                unit=None,
                state_class=None,
            ),
        ),
        _record(
            "text",
            capability=_capability(
                "text",
                kind=CapabilityKind.TEXT,
                value="ready",
                semantic=None,
                unit=None,
                state_class=None,
            ),
        ),
        _record(
            "unknown",
            capability=_capability(
                "unknown",
                value=None,
                availability=Availability.UNKNOWN,
            ),
        ),
        _record("stale", capability=stale_capability, stale=True),
        _record("pending", pending=True),
    ]
    catalog = TargetCatalog(records)

    encoded = json.dumps(catalog.to_dict(), allow_nan=False)

    assert TargetCatalog.from_dict(json.loads(encoded)) == catalog


def test_v2_catalog_migrates_pending_to_false_without_mutating_input() -> None:
    """Released v2 records become confirmed records when loaded by v3."""
    document = TargetCatalog([_record("sensor-1", pending=True)]).to_dict()
    document["version"] = 2
    document["targets"][0].pop("pending")
    original = deepcopy(document)

    catalog = TargetCatalog.from_dict(document)

    assert catalog.records == (_record("sensor-1", pending=False),)
    assert catalog.to_dict()["version"] == CATALOG_SCHEMA_VERSION
    assert catalog.to_dict()["targets"][0]["pending"] is False
    assert document == original


def test_v2_catalog_rejects_v3_pending_field() -> None:
    """Version selection cannot be used to weaken exact record fields."""
    document = TargetCatalog([_record("sensor-1")]).to_dict()
    document["version"] = 2

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(document)


def test_v3_catalog_rejects_pending_stale_record() -> None:
    """A target cannot be both awaiting confirmation and known stale."""
    capability = _capability(
        "sensor-1",
        value=None,
        availability=Availability.UNAVAILABLE,
    )
    document = TargetCatalog(
        [_record("sensor-1", capability=capability, stale=True)]
    ).to_dict()
    document["targets"][0]["pending"] = True

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(document)


def test_serialized_target_order_is_deterministic() -> None:
    """Equivalent catalogs produce byte-for-byte stable JSON data ordering."""
    catalog = TargetCatalog([_record("sensor-b"), _record("sensor-a")])

    assert [
        target["capability"]["source"]["object_id"]
        for target in catalog.to_dict()["targets"]
    ] == ["sensor-a", "sensor-b"]


def test_none_alone_represents_no_persisted_catalog() -> None:
    """Absence is distinct from present but corrupt persisted data."""
    assert catalog_from_document(None) == TargetCatalog()
    with pytest.raises(CatalogFormatError):
        TargetCatalog.from_dict(None)
    with pytest.raises(CatalogFormatError):
        catalog_from_document({})


def test_released_v1_is_rejected_without_mutating_the_document() -> None:
    """The released pre-state-class schema is not silently reinterpreted."""
    document = TargetCatalog([_record("sensor-1")]).to_dict()
    document["version"] = 1
    document["targets"][0].pop("pending")
    document["targets"][0]["capability"].pop("state_class")
    original = deepcopy(document)

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(document)

    assert document == original


def test_unknown_future_version_is_rejected_without_mutating_the_document() -> None:
    """An older reader leaves a future catalog available to newer software."""
    document = TargetCatalog([_record("sensor-1")]).to_dict()
    document["version"] = CATALOG_SCHEMA_VERSION + 1
    original = deepcopy(document)

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(document)

    assert document == original


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.pop("version"),
        lambda data: data.update(version=0),
        lambda data: data.update(version=True),
        lambda data: data.update(version=3.0),
        lambda data: data.update(extra=True),
        lambda data: data.update(targets={}),
        lambda data: data["targets"].append("not-a-record"),
        lambda data: data["targets"][0].pop("stale"),
        lambda data: data["targets"][0].pop("pending"),
        lambda data: data["targets"][0].update(extra=True),
        lambda data: data["targets"][0].update(stale=1),
        lambda data: data["targets"][0].update(pending=1),
        lambda data: data["targets"][0].update(target_id=" "),
        lambda data: data["targets"][0]["capability"].pop("unit"),
        lambda data: data["targets"][0]["capability"].pop("state_class"),
        lambda data: data["targets"][0]["capability"].update(extra=True),
        lambda data: data["targets"][0]["capability"].update(kind="other"),
        lambda data: data["targets"][0]["capability"].update(availability="other"),
        lambda data: data["targets"][0]["capability"].update(value=True),
        lambda data: data["targets"][0]["capability"].update(state_class=1),
        lambda data: data["targets"][0]["capability"]["source"].pop("system"),
        lambda data: data["targets"][0]["capability"]["source"].update(extra=True),
        lambda data: data["targets"][0]["capability"]["source"].update(object_id=1),
    ],
)
def test_malformed_or_unsupported_data_raises_one_safe_error(mutate: Any) -> None:
    """All corrupt schema shapes fail closed behind the persistence boundary."""
    data = TargetCatalog([_record("sensor-1")]).to_dict()
    mutate(data)

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(data)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_raise_safe_format_error(value: float) -> None:
    """Non-standard JSON numeric values never enter the target catalog."""
    record = _serialized_record()
    record["capability"]["value"] = value

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(
            {"version": CATALOG_SCHEMA_VERSION, "targets": [record]}
        )


def test_deserialization_wraps_duplicate_source_identity() -> None:
    """Persisted source ambiguity fails closed rather than choosing a target."""
    record = _serialized_record()
    duplicate = deepcopy(record)
    duplicate["target_id"] = "target-2"

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(
            {
                "version": CATALOG_SCHEMA_VERSION,
                "targets": [record, duplicate],
            }
        )


def test_deserialization_wraps_duplicate_target_id() -> None:
    """Persisted target ambiguity fails closed rather than losing a mapping."""
    record = _serialized_record()
    duplicate = deepcopy(record)
    duplicate["capability"]["source"]["object_id"] = "sensor-2"

    with pytest.raises(CatalogFormatError, match="^invalid target catalog$"):
        TargetCatalog.from_dict(
            {
                "version": CATALOG_SCHEMA_VERSION,
                "targets": [record, duplicate],
            }
        )


def test_compound_capability_serialization_round_trip() -> None:
    """A catalog with a CompoundCapability serializes and deserializes accurately."""
    source = SourceIdentity("home_assistant", "instance-1", "climate-1", "temp_hum")
    cap1 = Capability(
        SourceIdentity("home_assistant", "instance-1", "climate-1", "temperature"),
        CapabilityKind.NUMERIC,
        "Temperature",
        21.5,
        Availability.AVAILABLE,
        semantic="temperature",
        unit="celsius",
    )
    cap2 = Capability(
        SourceIdentity("home_assistant", "instance-1", "climate-1", "humidity"),
        CapabilityKind.NUMERIC,
        "Humidity",
        50.0,
        Availability.AVAILABLE,
        semantic="humidity",
        unit="percent",
    )
    compound = CompoundCapability(source, "Climate", (cap1, cap2))
    record = TargetRecord("target-compound", compound)

    catalog = TargetCatalog((record,))
    serialized = catalog_to_document(catalog)

    # Verify serialized structure
    assert serialized["version"] == CATALOG_SCHEMA_VERSION
    assert len(serialized["targets"]) == 1
    serialized_cap = serialized["targets"][0]["capability"]
    assert serialized_cap["kind"] == "compound"
    assert serialized_cap["name"] == "Climate"
    assert len(serialized_cap["capabilities"]) == 2
    assert serialized_cap["capabilities"][0]["name"] == "Temperature"
    assert serialized_cap["capabilities"][1]["name"] == "Humidity"

    # Verify deserialization
    deserialized = catalog_from_document(serialized)
    assert len(deserialized) == 1
    deserialized_record = deserialized.records[0]
    assert deserialized_record.target_id == "target-compound"
    assert isinstance(deserialized_record.capability, CompoundCapability)
    assert deserialized_record.capability.name == "Climate"
    assert len(deserialized_record.capability.capabilities) == 2
    assert deserialized_record.capability.capabilities[0].value == 21.5
    assert deserialized_record.capability.capabilities[1].value == 50.0
