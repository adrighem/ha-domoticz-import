"""Transport-neutral planning for synchronizing capability snapshots.

This module intentionally uses syntax supported by Python 3.9 so it can be
vendored into Domoticz releases independently of the Home Assistant adapter.
"""

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, Iterable, Optional, Set, Tuple, Union

from .capabilities import Availability, Capability, CompoundCapability, SourceIdentity

_MAX_CANONICAL_IDENTITY_BYTES = 64 * 1024


@dataclass(frozen=True)
class SourceScope:
    """One source-system instance reconciled as an isolated snapshot."""

    system: str
    instance_id: str

    def __post_init__(self) -> None:
        """Validate scope components."""
        for field_name in ("system", "instance_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string")
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
            if value != value.strip():
                raise ValueError(f"{field_name} must not have surrounding whitespace")

    def contains(self, source: SourceIdentity) -> bool:
        """Return whether an identity belongs to this source instance."""
        return source.system == self.system and source.instance_id == self.instance_id


class ReconciliationActionKind(str, Enum):
    """Changes a target adapter may apply."""

    CREATE = "create"
    UPDATE = "update"
    MARK_UNAVAILABLE = "mark_unavailable"


class TargetBindingError(ValueError):
    """A persisted or planned target identity is not safely attributable."""


def _validate_target_id(target_id: object) -> None:
    """Validate an opaque identifier allocated by a target adapter."""
    if not isinstance(target_id, str):
        raise TypeError("target_id must be a string")
    if not target_id.strip():
        raise ValueError("target_id must not be empty")
    if target_id != target_id.strip():
        raise ValueError("target_id must not have surrounding whitespace")


def derive_domoticz_target_id(source: SourceIdentity) -> str:
    """Derive the released Domoticz DeviceID from complete source provenance."""
    if not isinstance(source, SourceIdentity):
        raise TypeError("source must be a SourceIdentity")
    if any(
        0xD800 <= ord(character) <= 0xDFFF
        for component in source.key
        for character in component
    ):
        raise ValueError("source identity contains invalid Unicode")
    identity = json.dumps(
        {
            "system": source.system,
            "instance_id": source.instance_id,
            "object_id": source.object_id,
            "capability_id": source.capability_id,
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(identity) > _MAX_CANONICAL_IDENTITY_BYTES:
        raise ValueError("source identity exceeds the canonical size limit")
    digest = hashlib.sha256(identity).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")
    return "HA" + encoded[:23]


@dataclass(frozen=True)
class TargetObservation:
    """One hardware-scoped target identity and its observed unit numbers."""

    target_id: str
    units: Tuple[int, ...]

    def __post_init__(self) -> None:
        """Require an immutable, canonical, duplicate-free unit identity."""
        _validate_target_id(self.target_id)
        if type(self.units) is not tuple:
            raise TypeError("units must be a tuple")
        for unit in self.units:
            if type(unit) is not int:
                raise TypeError("units must contain integers")
            if not 1 <= unit <= 255:
                raise ValueError("unit must be between 1 and 255")
        if self.units != tuple(sorted(self.units)) or len(self.units) != len(
            set(self.units)
        ):
            raise ValueError("units must be sorted and duplicate-free")


@dataclass(frozen=True)
class TargetRecord:
    """The last capability snapshot associated with a target object."""

    target_id: str
    capability: Union[Capability, CompoundCapability]
    stale: bool = False
    pending: bool = False

    def __post_init__(self) -> None:
        """Validate persisted target state before planning."""
        _validate_target_id(self.target_id)
        if not isinstance(self.capability, (Capability, CompoundCapability)):
            raise TypeError("capability must be a Capability or CompoundCapability")
        _validate_stale(self.stale, self.capability)
        if not isinstance(self.pending, bool):
            raise TypeError("pending must be a bool")
        if self.pending and self.stale:
            raise ValueError("pending records must not be stale")


@dataclass(frozen=True)
class ReconciliationAction:
    """One target-neutral synchronization operation."""

    kind: ReconciliationActionKind
    capability: Union[Capability, CompoundCapability]
    target_id: Optional[str] = None
    stale: bool = False

    def __post_init__(self) -> None:
        """Keep invalid operations out of target adapters."""
        if not isinstance(self.kind, ReconciliationActionKind):
            raise TypeError("kind must be a ReconciliationActionKind")
        if not isinstance(self.capability, (Capability, CompoundCapability)):
            raise TypeError("capability must be a Capability or CompoundCapability")
        _validate_stale(self.stale, self.capability)

        if self.kind is ReconciliationActionKind.CREATE:
            if self.target_id is not None:
                raise ValueError("create actions must not have a target_id")
        else:
            _validate_target_id(self.target_id)

        if self.stale and self.kind is not ReconciliationActionKind.MARK_UNAVAILABLE:
            raise ValueError("only mark-unavailable actions may be stale")
        if (
            self.kind is ReconciliationActionKind.UPDATE
            and self.capability.availability is not Availability.AVAILABLE
        ):
            raise ValueError("update actions require an available capability")
        if (
            self.kind is ReconciliationActionKind.MARK_UNAVAILABLE
            and self.capability.availability is Availability.AVAILABLE
        ):
            raise ValueError(
                "mark-unavailable actions require a non-available capability"
            )


def _validate_stale(
    stale: object, capability: Union[Capability, CompoundCapability]
) -> None:
    """Validate whether a capability is absent from its source snapshot."""
    if not isinstance(stale, bool):
        raise TypeError("stale must be a bool")
    if stale and capability.availability is not Availability.UNAVAILABLE:
        raise ValueError("stale records require an unavailable capability")


def _index_capabilities(
    capabilities: Iterable[Union[Capability, CompoundCapability]],
    scope: SourceScope,
) -> Dict[SourceIdentity, Union[Capability, CompoundCapability]]:
    """Index a snapshot and reject ambiguous source identities."""
    indexed: Dict[SourceIdentity, Union[Capability, CompoundCapability]] = {}
    for capability in capabilities:
        if not isinstance(capability, (Capability, CompoundCapability)):
            raise TypeError(
                "current capabilities must be Capability or CompoundCapability values"
            )
        if not scope.contains(capability.source):
            raise ValueError(
                f"current capability is outside source scope: {capability.source.key!r}"
            )
        if capability.source in indexed:
            raise ValueError(f"duplicate source identity: {capability.source.key!r}")
        indexed[capability.source] = capability
    return indexed


def _index_targets(
    targets: Iterable[TargetRecord],
    scope: SourceScope,
) -> Dict[SourceIdentity, TargetRecord]:
    """Index known targets and reject ambiguous source or target identities."""
    indexed: Dict[SourceIdentity, TargetRecord] = {}
    source_identities: Set[SourceIdentity] = set()
    target_ids: Set[str] = set()
    for target in targets:
        if not isinstance(target, TargetRecord):
            raise TypeError("known targets must be TargetRecord values")
        source = target.capability.source
        if source in source_identities:
            raise ValueError(f"duplicate source identity: {source.key!r}")
        if target.target_id in target_ids:
            raise ValueError(f"duplicate target_id: {target.target_id!r}")
        source_identities.add(source)
        target_ids.add(target.target_id)
        if scope.contains(source):
            indexed[source] = target
    return indexed


def validate_deterministic_target_bindings(
    targets: Iterable[TargetRecord],
) -> None:
    """Reject ambiguous or redirected bindings across one or more catalogs."""
    source_identities: Set[SourceIdentity] = set()
    target_ids: Set[str] = set()
    for target in targets:
        if not isinstance(target, TargetRecord):
            raise TypeError("target bindings must be TargetRecord values")
        source = target.capability.source
        if source in source_identities:
            raise TargetBindingError("duplicate source identity")
        if target.target_id in target_ids:
            raise TargetBindingError("duplicate target identity")
        if target.target_id != derive_domoticz_target_id(source):
            raise TargetBindingError("catalog lacks deterministic target identity")
        source_identities.add(source)
        target_ids.add(target.target_id)


def _index_observations(
    observations: Optional[Iterable[TargetObservation]],
) -> Optional[Dict[str, TargetObservation]]:
    """Index an optional authoritative inventory without guessing duplicates."""
    if observations is None:
        return None
    indexed: Dict[str, TargetObservation] = {}
    for observation in observations:
        if not isinstance(observation, TargetObservation):
            raise TypeError("observations must be TargetObservation values")
        if observation.target_id in indexed:
            raise TargetBindingError("duplicate observed target identity")
        indexed[observation.target_id] = observation
    return indexed


def validate_deterministic_target_ownership(
    current: Iterable[Union[Capability, CompoundCapability]],
    targets: Iterable[TargetRecord],
) -> None:
    """Validate ownership across current capabilities and one or more catalogs."""
    target_records = tuple(targets)
    validate_deterministic_target_bindings(target_records)
    catalog_kinds = {
        target.capability.source: target.capability.kind for target in target_records
    }

    source_identities: Set[SourceIdentity] = set()
    for capability in current:
        if not isinstance(capability, (Capability, CompoundCapability)):
            raise TypeError(
                "current capabilities must be Capability or CompoundCapability values"
            )
        if capability.source in source_identities:
            raise TargetBindingError("duplicate current source identity")
        catalog_kind = catalog_kinds.get(capability.source)
        if catalog_kind is not None and capability.kind is not catalog_kind:
            raise TargetBindingError("source capability kind conflicts with catalog")
        source_identities.add(capability.source)
    source_identities.update(target.capability.source for target in target_records)

    sources_by_target_id: Dict[str, SourceIdentity] = {}
    for source in source_identities:
        target_id = derive_domoticz_target_id(source)
        existing_source = sources_by_target_id.get(target_id)
        if existing_source is not None and existing_source != source:
            raise TargetBindingError("duplicate deterministic target identity")
        sources_by_target_id[target_id] = source


def _unavailable(
    capability: Union[Capability, CompoundCapability],
) -> Union[Capability, CompoundCapability]:
    """Return an unavailable snapshot while preserving capability metadata."""
    if isinstance(capability, CompoundCapability):
        return replace(
            capability,
            availability=Availability.UNAVAILABLE,
        )
    return replace(
        capability,
        value=None,
        availability=Availability.UNAVAILABLE,
    )


def plan_reconciliation(
    scope: SourceScope,
    current: Iterable[Union[Capability, CompoundCapability]],
    known_targets: Iterable[TargetRecord],
    observations: Optional[Iterable[TargetObservation]] = None,
) -> Tuple[ReconciliationAction, ...]:
    """Plan deterministic target changes without ever deleting a target.

    A target adapter allocates and persists a target ID while applying CREATE.
    UPDATE and MARK_UNAVAILABLE retain the opaque ID from the known target.
    """
    if not isinstance(scope, SourceScope):
        raise TypeError("scope must be a SourceScope")

    current_snapshot = tuple(current)
    known_target_records = tuple(known_targets)
    current_by_source = _index_capabilities(current_snapshot, scope)
    targets_by_source = _index_targets(known_target_records, scope)
    observed_by_target_id = _index_observations(observations)
    if observed_by_target_id is not None:
        validate_deterministic_target_ownership(
            current_snapshot,
            known_target_records,
        )
    source_identities = set(current_by_source) | set(targets_by_source)

    actions = []
    for source in sorted(source_identities, key=lambda identity: identity.key):
        capability = current_by_source.get(source)
        target = targets_by_source.get(source)

        if target is None:
            assert capability is not None
            if (
                observed_by_target_id is not None
                and derive_domoticz_target_id(source) in observed_by_target_id
            ):
                continue
            actions.append(
                ReconciliationAction(
                    kind=ReconciliationActionKind.CREATE,
                    capability=capability,
                )
            )
        elif observed_by_target_id is not None:
            observation = observed_by_target_id.get(target.target_id)
            if observation is not None and observation.units not in {(), (1,)}:
                continue
            if capability is None:
                if observation is None or observation.units == ():
                    continue
                actions.append(
                    ReconciliationAction(
                        kind=ReconciliationActionKind.MARK_UNAVAILABLE,
                        target_id=target.target_id,
                        capability=_unavailable(target.capability),
                        stale=True,
                    )
                )
                continue
            kind = (
                ReconciliationActionKind.UPDATE
                if capability.availability is Availability.AVAILABLE
                else ReconciliationActionKind.MARK_UNAVAILABLE
            )
            actions.append(
                ReconciliationAction(
                    kind=kind,
                    target_id=target.target_id,
                    capability=capability,
                )
            )
        elif capability is None:
            actions.append(
                ReconciliationAction(
                    kind=ReconciliationActionKind.MARK_UNAVAILABLE,
                    target_id=target.target_id,
                    capability=_unavailable(target.capability),
                    stale=True,
                )
            )
        elif (
            target.pending
            or target.stale
            or capability != target.capability
            or capability.availability is not Availability.AVAILABLE
        ):
            kind = (
                ReconciliationActionKind.UPDATE
                if capability.availability is Availability.AVAILABLE
                else ReconciliationActionKind.MARK_UNAVAILABLE
            )
            actions.append(
                ReconciliationAction(
                    kind=kind,
                    target_id=target.target_id,
                    capability=capability,
                )
            )

    return tuple(actions)
