"""Confirmation-based execution of transport-neutral reconciliation plans.

This module intentionally uses syntax supported by Python 3.9 so it can be
vendored into Domoticz releases independently of the Home Assistant adapter.
"""

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Optional, Protocol, Tuple

from .capabilities import Capability, SourceIdentity
from .catalog import (
    CatalogFormatError,
    TargetCatalog,
    catalog_from_document,
    catalog_to_document,
)
from .reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
    SourceScope,
    TargetObservation,
    TargetRecord,
    derive_domoticz_target_id,
    plan_reconciliation,
)


class CatalogStorageError(RuntimeError):
    """A catalog load or atomic save could not be confirmed."""


class TargetAdapterError(RuntimeError):
    """The target adapter cannot safely continue this reconciliation."""


class TargetActionError(TargetAdapterError):
    """One target action failed without invalidating unrelated actions."""


class ExecutionConflictError(RuntimeError):
    """The plan, catalog, or target confirmation disagree."""


def _validate_target_id(target_id: object) -> None:
    """Validate an opaque target ID without exposing platform details."""
    if not isinstance(target_id, str):
        raise TypeError("target_id must be a string")
    if not target_id.strip():
        raise ValueError("target_id must not be empty")
    if target_id != target_id.strip():
        raise ValueError("target_id must not have surrounding whitespace")


@dataclass(frozen=True)
class ApplyConfirmation:
    """A target's confirmation that it converged to an action's desired state."""

    target_id: str
    source: SourceIdentity

    def __post_init__(self) -> None:
        """Validate target confirmation before it reaches the catalog."""
        _validate_target_id(self.target_id)
        if not isinstance(self.source, SourceIdentity):
            raise TypeError("source must be a SourceIdentity")


class TargetAdapter(Protocol):
    """Idempotent target operations for one destination instance.

    CREATE must ensure exactly one target for ``action.capability.source`` and
    adopt an existing match after a retry. All actions set complete desired
    state and may be repeated. The method returns only after confirmation.
    """

    async def async_apply(
        self,
        action: ReconciliationAction,
    ) -> ApplyConfirmation:
        """Apply or safely repeat one desired target state."""
        raise NotImplementedError


class CatalogStorage(Protocol):
    """Atomic catalog storage namespaced to one destination instance."""

    async def async_load(self) -> Optional[Mapping[str, object]]:
        """Load a complete catalog document, or None when never saved."""
        raise NotImplementedError

    async def async_save(self, document: Mapping[str, object]) -> None:
        """Atomically replace the complete catalog document."""
        raise NotImplementedError


class ExecutionStatus(str, Enum):
    """Outcome of one attempted reconciliation action."""

    COMMITTED = "committed"
    TARGET_NOT_CONFIRMED = "target_not_confirmed"
    APPLIED_NOT_COMMITTED = "applied_not_committed"


@dataclass(frozen=True)
class ActionExecutionResult:
    """Sanitized outcome for one attempted action, without exception details."""

    action: ReconciliationAction
    status: ExecutionStatus
    target_id: Optional[str] = None

    def __post_init__(self) -> None:
        """Keep reports internally consistent and free of error details."""
        if not isinstance(self.action, ReconciliationAction):
            raise TypeError("action must be a ReconciliationAction")
        if not isinstance(self.status, ExecutionStatus):
            raise TypeError("status must be an ExecutionStatus")
        if self.target_id is not None:
            _validate_target_id(self.target_id)

        if self.status is ExecutionStatus.TARGET_NOT_CONFIRMED:
            if self.target_id is not None:
                raise ValueError("unconfirmed results must not have a target ID")
        elif self.target_id is None:
            raise ValueError("confirmed target results require a target ID")


@dataclass(frozen=True)
class ExecutionReport:
    """Result of one serialized load, plan, apply, and save cycle."""

    actions: Tuple[ReconciliationAction, ...]
    results: Tuple[ActionExecutionResult, ...]
    catalog: TargetCatalog
    persistence_uncertain: bool = False

    def __post_init__(self) -> None:
        """Validate report structure."""
        if not isinstance(self.actions, tuple):
            raise TypeError("actions must be a tuple")
        if not isinstance(self.results, tuple):
            raise TypeError("results must be a tuple")
        if not isinstance(self.catalog, TargetCatalog):
            raise TypeError("catalog must be a TargetCatalog")
        if not isinstance(self.persistence_uncertain, bool):
            raise TypeError("persistence_uncertain must be a bool")
        if len(self.results) > len(self.actions):
            raise ValueError("results cannot outnumber planned actions")

    @property
    def remaining_actions(self) -> Tuple[ReconciliationAction, ...]:
        """Return actions not attempted after a global persistence failure."""
        return self.actions[len(self.results) :]


def _preflight_actions(
    catalog: TargetCatalog,
    actions: Tuple[ReconciliationAction, ...],
) -> None:
    """Reject stale or ambiguous plans before the first target side effect."""
    sources = set()
    for action in actions:
        if not isinstance(action, ReconciliationAction):
            raise TypeError("actions must contain ReconciliationAction values")
        source = action.capability.source
        if source in sources:
            raise ExecutionConflictError(
                f"multiple actions for source identity {source.key!r}"
            )
        sources.add(source)

        existing = catalog.get(source)
        if action.kind is ReconciliationActionKind.CREATE:
            if existing is not None:
                raise ExecutionConflictError(
                    f"create action already has target {existing.target_id!r}"
                )
            continue
        if existing is None:
            raise ExecutionConflictError(
                f"existing-target action has no catalog record for {source.key!r}"
            )
        if action.target_id != existing.target_id:
            raise ExecutionConflictError(
                f"action target does not match catalog for {source.key!r}"
            )


def _record_from_confirmation(
    action: ReconciliationAction,
    confirmation: ApplyConfirmation,
    *,
    inventory_mode: bool = False,
) -> TargetRecord:
    """Build trusted catalog state from an action and a minimal confirmation."""
    if not isinstance(confirmation, ApplyConfirmation):
        raise ExecutionConflictError("adapter returned an invalid confirmation")
    if confirmation.source != action.capability.source:
        raise ExecutionConflictError("adapter confirmed a different source identity")
    if (
        action.kind is not ReconciliationActionKind.CREATE
        and confirmation.target_id != action.target_id
    ):
        raise ExecutionConflictError("adapter confirmed a different target ID")
    if (
        inventory_mode
        and action.kind is ReconciliationActionKind.CREATE
        and confirmation.target_id
        != derive_domoticz_target_id(action.capability.source)
    ):
        raise ExecutionConflictError(
            "adapter confirmed a non-deterministic target ID for create"
        )
    return TargetRecord(
        target_id=confirmation.target_id,
        capability=action.capability,
        stale=action.stale,
    )


async def async_execute_reconciliation(
    catalog: TargetCatalog,
    actions: Iterable[ReconciliationAction],
    adapter: TargetAdapter,
    storage: CatalogStorage,
    *,
    inventory_mode: bool = False,
) -> ExecutionReport:
    """Apply a precomputed plan and commit each confirmed action atomically.

    Inventory-aware creates first persist their deterministic ownership intent.
    Expected entity-specific failures are isolated. A catalog save failure
    stops the batch because it leaves persistence success uncertain.
    Callers must serialize the complete load-plan-execute cycle.
    """
    if not isinstance(catalog, TargetCatalog):
        raise TypeError("catalog must be a TargetCatalog")
    if not isinstance(inventory_mode, bool):
        raise TypeError("inventory_mode must be a bool")
    planned_actions = tuple(actions)
    _preflight_actions(catalog, planned_actions)

    committed = catalog
    results = []
    persistence_uncertain = False

    for action in planned_actions:
        if inventory_mode and action.kind is ReconciliationActionKind.CREATE:
            try:
                pending_catalog = committed.with_record(
                    TargetRecord(
                        target_id=derive_domoticz_target_id(action.capability.source),
                        capability=action.capability,
                        pending=True,
                    )
                )
            except (CatalogFormatError, TypeError, ValueError) as error:
                raise ExecutionConflictError(
                    "pending ownership intent conflicts with the catalog"
                ) from error
            await storage.async_save(catalog_to_document(pending_catalog))
            committed = pending_catalog

        try:
            confirmation = await adapter.async_apply(action)
        except TargetActionError:
            results.append(
                ActionExecutionResult(
                    action=action,
                    status=ExecutionStatus.TARGET_NOT_CONFIRMED,
                )
            )
            continue

        record = _record_from_confirmation(
            action,
            confirmation,
            inventory_mode=inventory_mode,
        )
        try:
            candidate = committed.with_record(record)
        except (CatalogFormatError, TypeError, ValueError) as error:
            raise ExecutionConflictError(
                "confirmed target conflicts with the catalog"
            ) from error

        if candidate == committed:
            results.append(
                ActionExecutionResult(
                    action=action,
                    status=ExecutionStatus.COMMITTED,
                    target_id=confirmation.target_id,
                )
            )
            continue

        try:
            await storage.async_save(catalog_to_document(candidate))
        except CatalogStorageError:
            results.append(
                ActionExecutionResult(
                    action=action,
                    status=ExecutionStatus.APPLIED_NOT_COMMITTED,
                    target_id=confirmation.target_id,
                )
            )
            persistence_uncertain = True
            break

        committed = candidate
        results.append(
            ActionExecutionResult(
                action=action,
                status=ExecutionStatus.COMMITTED,
                target_id=confirmation.target_id,
            )
        )

    return ExecutionReport(
        actions=planned_actions,
        results=tuple(results),
        catalog=committed,
        persistence_uncertain=persistence_uncertain,
    )


class ReconciliationExecutor:
    """Serialize reconciliation for one destination catalog namespace."""

    def __init__(
        self,
        adapter: TargetAdapter,
        storage: CatalogStorage,
    ) -> None:
        """Initialize one executor and its catalog-wide lock."""
        self._adapter = adapter
        self._storage = storage
        self._lock = asyncio.Lock()

    async def async_reconcile(
        self,
        scope: SourceScope,
        current: Iterable[Capability],
        observations: Optional[Iterable[TargetObservation]] = None,
    ) -> ExecutionReport:
        """Load, plan, apply, and persist one authoritative source snapshot."""
        snapshot = tuple(current)
        observed_snapshot = None if observations is None else tuple(observations)
        async with self._lock:
            document = await self._storage.async_load()
            catalog = (
                TargetCatalog() if document is None else catalog_from_document(document)
            )
            actions = plan_reconciliation(
                scope,
                snapshot,
                catalog.records,
                observations=observed_snapshot,
            )
            return await async_execute_reconciliation(
                catalog,
                actions,
                self._adapter,
                self._storage,
                inventory_mode=observed_snapshot is not None,
            )
