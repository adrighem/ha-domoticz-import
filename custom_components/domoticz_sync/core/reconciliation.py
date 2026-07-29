"""Transport-neutral planning for synchronizing capability snapshots.

This module intentionally uses syntax supported by Python 3.9 so it can be
vendored into Domoticz releases independently of the Home Assistant adapter.
"""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, Iterable, Optional, Set, Tuple

from .capabilities import Availability, Capability, SourceIdentity


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


def _validate_target_id(target_id: object) -> None:
    """Validate an opaque identifier allocated by a target adapter."""
    if not isinstance(target_id, str):
        raise TypeError("target_id must be a string")
    if not target_id.strip():
        raise ValueError("target_id must not be empty")
    if target_id != target_id.strip():
        raise ValueError("target_id must not have surrounding whitespace")


@dataclass(frozen=True)
class TargetRecord:
    """The last capability snapshot associated with a target object."""

    target_id: str
    capability: Capability
    stale: bool = False

    def __post_init__(self) -> None:
        """Validate persisted target state before planning."""
        _validate_target_id(self.target_id)
        if not isinstance(self.capability, Capability):
            raise TypeError("capability must be a Capability")
        _validate_stale(self.stale, self.capability)


@dataclass(frozen=True)
class ReconciliationAction:
    """One target-neutral synchronization operation."""

    kind: ReconciliationActionKind
    capability: Capability
    target_id: Optional[str] = None
    stale: bool = False

    def __post_init__(self) -> None:
        """Keep invalid operations out of target adapters."""
        if not isinstance(self.kind, ReconciliationActionKind):
            raise TypeError("kind must be a ReconciliationActionKind")
        if not isinstance(self.capability, Capability):
            raise TypeError("capability must be a Capability")
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


def _validate_stale(stale: object, capability: Capability) -> None:
    """Validate whether a capability is absent from its source snapshot."""
    if not isinstance(stale, bool):
        raise TypeError("stale must be a bool")
    if stale and capability.availability is not Availability.UNAVAILABLE:
        raise ValueError("stale records require an unavailable capability")


def _index_capabilities(
    capabilities: Iterable[Capability],
    scope: SourceScope,
) -> Dict[SourceIdentity, Capability]:
    """Index a snapshot and reject ambiguous source identities."""
    indexed: Dict[SourceIdentity, Capability] = {}
    for capability in capabilities:
        if not isinstance(capability, Capability):
            raise TypeError("current capabilities must be Capability values")
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


def _unavailable(capability: Capability) -> Capability:
    """Return an unavailable snapshot while preserving capability metadata."""
    return replace(
        capability,
        value=None,
        availability=Availability.UNAVAILABLE,
    )


def plan_reconciliation(
    scope: SourceScope,
    current: Iterable[Capability],
    known_targets: Iterable[TargetRecord],
) -> Tuple[ReconciliationAction, ...]:
    """Plan deterministic target changes without ever deleting a target.

    A target adapter allocates and persists a target ID while applying CREATE.
    UPDATE and MARK_UNAVAILABLE retain the opaque ID from the known target.
    """
    if not isinstance(scope, SourceScope):
        raise TypeError("scope must be a SourceScope")

    current_by_source = _index_capabilities(current, scope)
    targets_by_source = _index_targets(known_targets, scope)
    source_identities = set(current_by_source) | set(targets_by_source)

    actions = []
    for source in sorted(source_identities, key=lambda identity: identity.key):
        capability = current_by_source.get(source)
        target = targets_by_source.get(source)

        if target is None:
            actions.append(
                ReconciliationAction(
                    kind=ReconciliationActionKind.CREATE,
                    capability=capability,
                )
            )
        elif capability is None:
            if target.stale:
                continue
            actions.append(
                ReconciliationAction(
                    kind=ReconciliationActionKind.MARK_UNAVAILABLE,
                    target_id=target.target_id,
                    capability=_unavailable(target.capability),
                    stale=True,
                )
            )
        elif target.stale or capability != target.capability:
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
