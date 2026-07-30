"""Versioned persistence for transport-neutral target records.

This module intentionally uses syntax supported by Python 3.9 so it can be
vendored into Domoticz releases independently of the Home Assistant adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .capabilities import Availability, Capability, CapabilityKind, SourceIdentity
from .reconciliation import TargetRecord

CATALOG_SCHEMA_VERSION = 2

_ROOT_KEYS = {"version", "targets"}
_TARGET_KEYS = {"target_id", "capability", "stale"}
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
_SOURCE_KEYS = {"system", "instance_id", "object_id", "capability_id"}
_CATALOG_PARSE_ERRORS = (KeyError, TypeError, ValueError, OverflowError)


class CatalogFormatError(ValueError):
    """A persisted target catalog is missing, unsupported, or malformed."""


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
        """Serialize the complete catalog to the JSON-compatible v2 schema."""
        targets: List[Dict[str, object]] = []
        for record in self._records:
            capability = record.capability
            source = capability.source
            targets.append(
                {
                    "target_id": record.target_id,
                    "capability": {
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
                    },
                    "stale": record.stale,
                }
            )
        return {
            "version": CATALOG_SCHEMA_VERSION,
            "targets": targets,
        }

    @classmethod
    def from_dict(cls, data: object) -> TargetCatalog:
        """Deserialize a strict v2 schema or raise one safe format error."""
        try:
            _require_object(data, _ROOT_KEYS)
            if type(data["version"]) is not int:
                raise ValueError("invalid schema version")
            if data["version"] != CATALOG_SCHEMA_VERSION:
                raise ValueError("unsupported schema version")

            targets = data["targets"]
            if not isinstance(targets, list):
                raise TypeError("targets must be a list")
            return cls(_record_from_dict(item) for item in targets)
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


def _record_from_dict(data: object) -> TargetRecord:
    """Build one fully validated record from its strict v2 representation."""
    _require_object(data, _TARGET_KEYS)
    capability_data = data["capability"]
    _require_object(capability_data, _CAPABILITY_KEYS)
    source_data = capability_data["source"]
    _require_object(source_data, _SOURCE_KEYS)

    source = SourceIdentity(
        system=source_data["system"],
        instance_id=source_data["instance_id"],
        object_id=source_data["object_id"],
        capability_id=source_data["capability_id"],
    )
    capability = Capability(
        source=source,
        kind=CapabilityKind(capability_data["kind"]),
        name=capability_data["name"],
        value=capability_data["value"],
        availability=Availability(capability_data["availability"]),
        semantic=capability_data["semantic"],
        unit=capability_data["unit"],
        state_class=capability_data["state_class"],
    )
    return TargetRecord(
        target_id=data["target_id"],
        capability=capability,
        stale=data["stale"],
    )
