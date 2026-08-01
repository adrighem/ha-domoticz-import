"""Versioned persistence for transport-neutral target records.

This module intentionally uses syntax supported by Python 3.9 so it can be
vendored into Domoticz releases independently of the Home Assistant adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .capabilities import (
    Availability,
    Capability,
    CapabilityKind,
    CompoundCapability,
    SourceIdentity,
)
from .reconciliation import TargetRecord

CATALOG_SCHEMA_VERSION = 3
_PREVIOUS_CATALOG_SCHEMA_VERSION = 2

_ROOT_KEYS = {"version", "targets"}
_TARGET_KEYS_V2 = {"target_id", "capability", "stale"}
_TARGET_KEYS_V3 = _TARGET_KEYS_V2 | {"pending"}
_CAPABILITY_KEYS = {
    "source",
    "kind",
    "name",
    "value",
    "availability",
    "semantic",
    "unit",
    "state_class",
}
_COMPOUND_CAPABILITY_KEYS = {
    "source",
    "kind",
    "name",
    "availability",
    "capabilities",
}
_SOURCE_KEYS = {"system", "instance_id", "object_id", "capability_id"}
_CATALOG_PARSE_ERRORS = (KeyError, TypeError, ValueError, OverflowError)


class CatalogFormatError(ValueError):
    """A persisted target catalog is missing, unsupported, or malformed."""


def _capability_to_dict(capability: Union[Capability, CompoundCapability]) -> Dict[str, object]:
    """Serialize a capability or compound capability."""
    source = capability.source
    if isinstance(capability, CompoundCapability):
        return {
            "source": {
                "system": source.system,
                "instance_id": source.instance_id,
                "object_id": source.object_id,
                "capability_id": source.capability_id,
            },
            "kind": capability.kind.value,
            "name": capability.name,
            "availability": capability.availability.value,
            "capabilities": [
                _capability_to_dict(cap)
                for cap in capability.capabilities
            ],
        }
    return {
        "source": {
            "system": source.system,
            "instance_id": source.instance_id,
            "object_id": source.object_id,
            "capability_id": source.capability_id,
        },
        "kind": capability.kind.value,
        "name": capability.name,
        "value": capability.value,
        "availability": capability.availability.value,
        "semantic": capability.semantic,
        "unit": capability.unit,
        "state_class": capability.state_class,
    }


@dataclass(frozen=True, init=False)
class TargetCatalog:
    """An immutable, globally unambiguous collection of target records."""

    _records: Tuple[TargetRecord, ...]

    def __init__(self, records: Iterable[TargetRecord] = ()) -> None:
        """Validate and store records in deterministic source-identity order."""
        normalized = tuple(records)
        source_identities: Set[SourceIdentity] = set()
        target_ids: Set[str] = set()

        for record in normalized:
            if not isinstance(record, TargetRecord):
                raise TypeError("catalog records must be TargetRecord values")
            source = record.capability.source
            if source in source_identities:
                raise ValueError(f"duplicate source identity: {source.key!r}")
            if record.target_id in target_ids:
                raise ValueError(f"duplicate target_id: {record.target_id!r}")
            source_identities.add(source)
            target_ids.add(record.target_id)

        object.__setattr__(
            self,
            "_records",
            tuple(
                sorted(
                    normalized,
                    key=lambda record: record.capability.source.key,
                )
            ),
        )

    @property
    def records(self) -> Tuple[TargetRecord, ...]:
        """Return all records in deterministic source-identity order."""
        return self._records

    def __iter__(self) -> Iterator[TargetRecord]:
        """Iterate over records in deterministic source-identity order."""
        return iter(self._records)

    def __len__(self) -> int:
        """Return the number of known targets."""
        return len(self._records)

    def get(self, source: SourceIdentity) -> Optional[TargetRecord]:
        """Look up the target associated with an exact source identity."""
        if not isinstance(source, SourceIdentity):
            raise TypeError("source must be a SourceIdentity")
        for record in self._records:
            if record.capability.source == source:
                return record
        return None

    def with_record(self, record: TargetRecord) -> TargetCatalog:
        """Return a new catalog with one source mapping inserted or replaced."""
        if not isinstance(record, TargetRecord):
            raise TypeError("record must be a TargetRecord")

        source = record.capability.source
        retained = []
        for existing in self._records:
            if existing.capability.source == source:
                if existing.target_id != record.target_id:
                    raise ValueError(
                        "source identity is already bound to a different target_id"
                    )
                continue
            if existing.target_id == record.target_id:
                raise ValueError(f"duplicate target_id: {record.target_id!r}")
            retained.append(existing)
        retained.append(record)
        return TargetCatalog(retained)

    def upsert(self, record: TargetRecord) -> TargetCatalog:
        """Return a new catalog with one record, as an explicit upsert alias."""
        return self.with_record(record)

    def to_dict(self) -> Dict[str, object]:
        """Serialize the complete catalog to the JSON-compatible v3 schema."""
        targets: List[Dict[str, object]] = []
        for record in self._records:
            targets.append(
                {
                    "target_id": record.target_id,
                    "capability": _capability_to_dict(record.capability),
                    "stale": record.stale,
                    "pending": record.pending,
                }
            )
        return {
            "version": CATALOG_SCHEMA_VERSION,
            "targets": targets,
        }

    @classmethod
    def from_dict(cls, data: object) -> TargetCatalog:
        """Deserialize strict v2/v3 schemas or raise one safe format error."""
        try:
            _require_object(data, _ROOT_KEYS)
            if type(data["version"]) is not int:
                raise ValueError("invalid schema version")
            version = data["version"]
            if version not in (
                _PREVIOUS_CATALOG_SCHEMA_VERSION,
                CATALOG_SCHEMA_VERSION,
            ):
                raise ValueError("unsupported schema version")

            targets = data["targets"]
            if not isinstance(targets, list):
                raise TypeError("targets must be a list")
            return cls(_record_from_dict(item, version) for item in targets)
        except _CATALOG_PARSE_ERRORS:
            raise CatalogFormatError("invalid target catalog") from None


def catalog_from_document(document: object) -> TargetCatalog:
    """Load a document, treating only None as an absent persisted catalog."""
    if document is None:
        return TargetCatalog()
    return TargetCatalog.from_dict(document)


def catalog_to_document(catalog: TargetCatalog) -> Dict[str, object]:
    """Return a complete JSON-compatible document for one catalog."""
    if not isinstance(catalog, TargetCatalog):
        raise TypeError("catalog must be a TargetCatalog")
    return catalog.to_dict()


def _require_object(value: object, expected_keys: Set[str]) -> None:
    """Require one JSON object with exactly the expected fields."""
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("invalid object fields")


def _capability_from_dict(data: object) -> Union[Capability, CompoundCapability]:
    """Deserialize a capability or compound capability."""
    if not isinstance(data, dict):
        raise TypeError("capability must be a dict")
    kind_str = data.get("kind")
    if kind_str == CapabilityKind.COMPOUND.value:
        _require_object(data, _COMPOUND_CAPABILITY_KEYS)
        source_data = data["source"]
        _require_object(source_data, _SOURCE_KEYS)
        source = SourceIdentity(
            system=source_data["system"],
            instance_id=source_data["instance_id"],
            object_id=source_data["object_id"],
            capability_id=source_data["capability_id"],
        )
        nested_list = data["capabilities"]
        if not isinstance(nested_list, list):
            raise TypeError("capabilities must be a list")
        capabilities = tuple(_capability_from_dict(item) for item in nested_list)
        for cap in capabilities:
            if not isinstance(cap, Capability):
                raise TypeError("compound capability nested list must contain Capability values")
        return CompoundCapability(
            source=source,
            name=data["name"],
            capabilities=capabilities,
            availability=Availability(data["availability"]),
        )

    # Standard Capability
    _require_object(data, _CAPABILITY_KEYS)
    source_data = data["source"]
    _require_object(source_data, _SOURCE_KEYS)
    source = SourceIdentity(
        system=source_data["system"],
        instance_id=source_data["instance_id"],
        object_id=source_data["object_id"],
        capability_id=source_data["capability_id"],
    )
    return Capability(
        source=source,
        kind=CapabilityKind(data["kind"]),
        name=data["name"],
        value=data["value"],
        availability=Availability(data["availability"]),
        semantic=data["semantic"],
        unit=data["unit"],
        state_class=data["state_class"],
    )


def _record_from_dict(data: object, version: int) -> TargetRecord:
    """Build one fully validated record, migrating v2 pending state safely."""
    target_keys = (
        _TARGET_KEYS_V2
        if version == _PREVIOUS_CATALOG_SCHEMA_VERSION
        else _TARGET_KEYS_V3
    )
    _require_object(data, target_keys)
    capability = _capability_from_dict(data["capability"])
    return TargetRecord(
        target_id=data["target_id"],
        capability=capability,
        stale=data["stale"],
        pending=(
            False if version == _PREVIOUS_CATALOG_SCHEMA_VERSION else data["pending"]
        ),
    )
