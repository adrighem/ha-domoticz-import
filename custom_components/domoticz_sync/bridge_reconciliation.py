"""Home Assistant export reconciliation for the Domoticz bridge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from homeassistant.core import HomeAssistant
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .catalog_storage import (
    HomeAssistantBinaryCatalogStorage,
    HomeAssistantCatalogStorage,
)
from .const import CONF_EXPORT_LABEL_ID
from .core.capabilities import Capability, CapabilityKind, SourceIdentity
from .core.catalog import CatalogFormatError, TargetCatalog, catalog_from_document
from .core.execution import (
    ApplyConfirmation,
    CatalogStorage,
    CatalogStorageError,
    ExecutionConflictError,
    ExecutionReport,
    ExecutionStatus,
    ReconciliationExecutor,
    TargetActionError,
    async_execute_reconciliation,
)
from .core.protocol import (
    FEATURE_DOMOTICZ_INVENTORY_V1,
    FEATURE_HA_EXPORT_BINARY_V1,
    FEATURE_HA_EXPORT_CONTINUOUS_V1,
    FEATURE_HA_EXPORT_NUMERIC_V1,
    INVENTORY_TIMEOUT_SECONDS,
    MAX_INVENTORY_PAGES,
    MAX_INVENTORY_TARGETS,
    MAX_INVENTORY_UNITS,
    ApplyResult,
    ApplyResultStatus,
    InventoryResult,
    InventoryTarget,
    ProtocolError,
    ProtocolFormatError,
    assemble_inventory_results,
    build_apply,
    build_binary_apply,
    build_inventory_request,
    generate_request_id,
    parse_apply_result,
    parse_binary_apply_result,
    parse_inventory_result,
    validate_nonce,
)
from .core.reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
    SourceScope,
    TargetBindingError,
    TargetObservation,
    TargetRecord,
    derive_domoticz_target_id,
    plan_reconciliation,
    validate_deterministic_target_ownership,
)
from .home_assistant_source import (
    ExportExclusion,
    ExportLabelNotFoundError,
    async_subscribe_export_changes,
    collect_export_selection,
)

if TYPE_CHECKING:
    from .bridge import BridgeApplicationSession

_LOGGER = logging.getLogger(__name__)

APPLY_TIMEOUT = 10.0
CONTINUOUS_COALESCE_SECONDS = 0.25
INVENTORY_TIMEOUT = float(INVENTORY_TIMEOUT_SECONDS)
_SOURCE_SYSTEM = "home_assistant"


class _ContinuousDirtySignal:
    """Track value-free changes without retaining event payload data."""

    def __init__(self) -> None:
        """Initialize one session-local dirty generation."""
        self.event = asyncio.Event()
        self.generation = 0
        self._active = True

    def mark_dirty(self) -> None:
        """Record a change while the owning application session is active."""
        if not self._active:
            return
        self.generation += 1
        self.event.set()

    def deactivate(self) -> None:
        """Make already queued callbacks inert when the session ends."""
        self._active = False


@dataclass(frozen=True)
class _InventoryAdmission:
    """One capacity-safe baseline and its session-local blocked identities."""

    capabilities: tuple[Capability, ...]
    blocked_sources: frozenset[SourceIdentity]
    blocked_durable_sources: frozenset[SourceIdentity]


class _PreloadedCatalogStorage:
    """Reuse one catalog document loaded during inventory preflight."""

    def __init__(
        self,
        storage: CatalogStorage,
        document: Mapping[str, object] | None,
    ) -> None:
        """Keep the delegate and its already validated load result."""
        self._storage = storage
        self._document = document
        self._loaded = False

    async def async_load(self) -> Mapping[str, object] | None:
        """Return the preflight document exactly once to its executor."""
        if self._loaded:
            raise CatalogStorageError("target catalog storage is unavailable")
        self._loaded = True
        return self._document

    async def async_save(self, document: Mapping[str, object]) -> None:
        """Delegate atomic persistence after inventory-aware execution."""
        await self._storage.async_save(document)


class DomoticzSessionTargetAdapter:
    """Apply numeric actions over one authenticated bridge session."""

    def __init__(self, session: BridgeApplicationSession) -> None:
        """Bind the adapter to one sequential application session."""
        self._session = session

    async def async_apply(
        self,
        action: ReconciliationAction,
    ) -> ApplyConfirmation:
        """Send one action and wait for its exact correlated result."""
        request_id = generate_request_id()

        async with asyncio.timeout(APPLY_TIMEOUT):
            await self._session.async_send(self._build_apply(request_id, action))
            while True:
                payload = await self._session.async_receive()
                if isinstance(payload, dict) and payload.get("type") == "ping":
                    ping_id = _parse_ping(payload)
                    await self._session.async_send({"id": ping_id, "type": "pong"})
                    continue

                result = self._parse_apply_result(payload)
                if result.request_id != request_id:
                    raise ProtocolError("invalid protocol message")
                if result.status is ApplyResultStatus.REJECTED:
                    raise TargetActionError("target action was rejected")

                if (
                    result.source != action.capability.source
                    or result.target_id is None
                ):
                    raise ProtocolError("invalid protocol message")
                if (
                    action.kind is not ReconciliationActionKind.CREATE
                    and result.target_id != action.target_id
                ):
                    raise ProtocolError("invalid protocol message")
                return ApplyConfirmation(result.target_id, result.source)

    def _build_apply(
        self,
        request_id: str,
        action: ReconciliationAction,
    ) -> dict[str, object]:
        """Build one numeric action request."""
        return build_apply(self._session.selection, request_id, action)

    def _parse_apply_result(self, payload: object) -> ApplyResult:
        """Parse one numeric action result."""
        return parse_apply_result(self._session.selection, payload)


class DomoticzBinarySessionTargetAdapter(DomoticzSessionTargetAdapter):
    """Apply binary actions over one authenticated bridge session."""

    def _build_apply(
        self,
        request_id: str,
        action: ReconciliationAction,
    ) -> dict[str, object]:
        """Build one binary action request."""
        return build_binary_apply(self._session.selection, request_id, action)

    def _parse_apply_result(self, payload: object) -> ApplyResult:
        """Parse one binary action result."""
        return parse_binary_apply_result(self._session.selection, payload)


class _ExportStorageFactory(Protocol):
    """Construct destination-scoped storage through a stable keyword API."""

    def __call__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
        destination_id: str,
    ) -> CatalogStorage:
        """Build storage for one configured destination."""
        raise NotImplementedError


@dataclass(frozen=True)
class _ExportKindStrategy:
    """Bind one negotiated export kind to its transport and storage adapters."""

    kind: CapabilityKind
    feature: str
    adapter_factory: Callable[[BridgeApplicationSession], DomoticzSessionTargetAdapter]
    storage_factory: _ExportStorageFactory


def _numeric_adapter_factory(
    session: BridgeApplicationSession,
) -> DomoticzSessionTargetAdapter:
    """Build the numeric adapter using the current module implementation."""
    return DomoticzSessionTargetAdapter(session)


def _binary_adapter_factory(
    session: BridgeApplicationSession,
) -> DomoticzSessionTargetAdapter:
    """Build the binary adapter using the current module implementation."""
    return DomoticzBinarySessionTargetAdapter(session)


def _numeric_storage_factory(
    hass: HomeAssistant,
    *,
    entry_id: str,
    destination_id: str,
) -> CatalogStorage:
    """Build numeric storage using the current module implementation."""
    return HomeAssistantCatalogStorage(
        hass,
        entry_id=entry_id,
        destination_id=destination_id,
    )


def _binary_storage_factory(
    hass: HomeAssistant,
    *,
    entry_id: str,
    destination_id: str,
) -> CatalogStorage:
    """Build binary storage using the current module implementation."""
    return HomeAssistantBinaryCatalogStorage(
        hass,
        entry_id=entry_id,
        destination_id=destination_id,
    )


_NUMERIC_EXPORT_STRATEGY = _ExportKindStrategy(
    CapabilityKind.NUMERIC,
    FEATURE_HA_EXPORT_NUMERIC_V1,
    _numeric_adapter_factory,
    _numeric_storage_factory,
)
_BINARY_EXPORT_STRATEGY = _ExportKindStrategy(
    CapabilityKind.BINARY,
    FEATURE_HA_EXPORT_BINARY_V1,
    _binary_adapter_factory,
    _binary_storage_factory,
)

# Execution remains numeric-first for wire compatibility. Catalog preflight remains
# binary-first so its construction and load ordering are unchanged.
_EXPORT_EXECUTION_STRATEGIES = (
    _NUMERIC_EXPORT_STRATEGY,
    _BINARY_EXPORT_STRATEGY,
)
_ALL_CATALOG_STRATEGIES = (
    _BINARY_EXPORT_STRATEGY,
    _NUMERIC_EXPORT_STRATEGY,
)


def _negotiated_export_strategies(
    session: BridgeApplicationSession,
) -> tuple[_ExportKindStrategy, ...]:
    """Return negotiated export strategies in stable execution order."""
    return tuple(
        strategy
        for strategy in _EXPORT_EXECUTION_STRATEGIES
        if session.supports(strategy.feature)
    )


class HomeAssistantExportApplication:
    """Reconcile negotiated labelled entities when a bridge session connects."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the Home Assistant instance used for source collection."""
        self._hass = hass
        self._destination_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._reported_exclusions: dict[
            tuple[str, str], frozenset[ExportExclusion]
        ] = {}

    async def async_connected(self, session: BridgeApplicationSession) -> None:
        """Run one fail-closed reconciliation for each negotiated capability kind."""
        strategies = _negotiated_export_strategies(session)
        inventory_enabled = session.supports(FEATURE_DOMOTICZ_INVENTORY_V1)
        continuous_enabled = session.supports(FEATURE_HA_EXPORT_CONTINUOUS_V1)
        if continuous_enabled and (not inventory_enabled or not strategies):
            raise ProtocolError("continuous export is unavailable")
        if not strategies:
            return

        key = (session.entry_id, session.destination_id)
        lock = self._destination_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._destination_locks[key] = lock
        async with lock:
            await self._async_connected_locked(
                session,
                strategies=strategies,
                inventory_enabled=inventory_enabled,
                continuous_enabled=continuous_enabled,
            )

    async def _async_connected_locked(
        self,
        session: BridgeApplicationSession,
        *,
        strategies: tuple[_ExportKindStrategy, ...],
        inventory_enabled: bool,
        continuous_enabled: bool,
    ) -> None:
        """Run one complete destination transaction under its application lock."""

        entry = self._hass.config_entries.async_get_entry(session.entry_id)
        if entry is None:
            raise ProtocolError("export reconciliation is unavailable")

        label_id = entry.data.get(CONF_EXPORT_LABEL_ID)
        if not isinstance(label_id, str) or not label_id:
            raise ProtocolError("export reconciliation is unavailable")

        dirty_signal: _ContinuousDirtySignal | None = None
        unsubscribe = None
        rejected_desired_records: dict[SourceIdentity, TargetRecord] = {}
        try:
            instance_id = await async_get_instance_id(self._hass)
            included_kinds = frozenset(strategy.kind for strategy in strategies)
            if continuous_enabled:
                dirty_signal = _ContinuousDirtySignal()
                unsubscribe = async_subscribe_export_changes(
                    self._hass,
                    label_id=label_id,
                    on_change=dirty_signal.mark_dirty,
                )
            collection = collect_export_selection(
                self._hass,
                instance_id=instance_id,
                label_id=label_id,
                included_kinds=included_kinds,
            )
            observations: tuple[TargetObservation, ...] | None = None
            staged_storages: dict[CapabilityKind, CatalogStorage] = {}
            catalogs: dict[CapabilityKind, TargetCatalog] = {}
            reconciliation_capabilities = collection.capabilities
            capacity_blocked_sources: set[SourceIdentity] = set()
            active_capacity_blocked_durable_sources: set[SourceIdentity] = set()
            fresh_inventory_required_sources: set[SourceIdentity] = set()
            if inventory_enabled:
                inventory = await _async_fetch_inventory(session)
                observations = tuple(
                    TargetObservation(
                        target.target_id,
                        tuple(unit.unit for unit in target.units),
                    )
                    for target in inventory
                )
                staged_storages, catalogs = await self._async_preload_catalogs(
                    session,
                    collection.capabilities,
                )
                admission = _admit_inventory_creates(
                    SourceScope(_SOURCE_SYSTEM, instance_id),
                    collection.capabilities,
                    observations,
                    catalogs,
                    included_kinds,
                )
                reconciliation_capabilities = admission.capabilities
                capacity_blocked_sources = set(admission.blocked_sources)
                current_sources = {
                    capability.source for capability in collection.capabilities
                }
                active_capacity_blocked_durable_sources = (
                    set(admission.blocked_durable_sources) & current_sources
                )
                fresh_inventory_required_sources = (
                    set(admission.blocked_durable_sources) - current_sources
                )
            self._report_exclusions(session, collection.exclusions)

            reports: list[ExecutionReport] = []
            for strategy in strategies:
                report = await self._async_reconcile_kind(
                    session,
                    instance_id,
                    reconciliation_capabilities,
                    strategy=strategy,
                    observations=observations,
                    storage=staged_storages.get(strategy.kind),
                )
                self._ensure_persistence_confirmed(report)
                reports.append(report)
                catalogs[strategy.kind] = report.catalog

            _update_rejected_desired_records(rejected_desired_records, reports)
            self._log_reports(reports)

            if dirty_signal is not None:
                catalog_sources = {
                    record.capability.source
                    for catalog in catalogs.values()
                    for record in catalog.records
                }
                blocked_sources = {
                    capability.source for capability in collection.capabilities
                } - catalog_sources
                blocked_sources.update(capacity_blocked_sources)
                await self._async_run_continuous(
                    session,
                    instance_id=instance_id,
                    label_id=label_id,
                    strategies=strategies,
                    included_kinds=included_kinds,
                    dirty_signal=dirty_signal,
                    blocked_sources=blocked_sources,
                    capacity_blocked_sources=capacity_blocked_sources,
                    active_capacity_blocked_durable_sources=(
                        active_capacity_blocked_durable_sources
                    ),
                    fresh_inventory_required_sources=(fresh_inventory_required_sources),
                    rejected_desired_records=rejected_desired_records,
                )
        except (
            CatalogFormatError,
            CatalogStorageError,
            ExecutionConflictError,
            ExportLabelNotFoundError,
            TargetBindingError,
        ) as error:
            raise ProtocolError("export reconciliation is unavailable") from error
        finally:
            if dirty_signal is not None:
                dirty_signal.deactivate()
            if unsubscribe is not None:
                unsubscribe()

    @staticmethod
    def _log_reports(reports: list[ExecutionReport]) -> None:
        """Log aggregate outcomes without exposing source state or identity."""
        committed = sum(
            result.status is ExecutionStatus.COMMITTED
            for report in reports
            for result in report.results
        )
        rejected = sum(
            result.status is ExecutionStatus.TARGET_NOT_CONFIRMED
            for report in reports
            for result in report.results
        )
        _LOGGER.info(
            "Domoticz export reconciliation completed: "
            "%d planned, %d committed, %d rejected",
            sum(len(report.actions) for report in reports),
            committed,
            rejected,
        )

    async def _async_run_continuous(
        self,
        session: BridgeApplicationSession,
        *,
        instance_id: str,
        label_id: str,
        strategies: tuple[_ExportKindStrategy, ...],
        included_kinds: frozenset[CapabilityKind],
        dirty_signal: _ContinuousDirtySignal,
        blocked_sources: set[SourceIdentity],
        capacity_blocked_sources: set[SourceIdentity],
        active_capacity_blocked_durable_sources: set[SourceIdentity],
        fresh_inventory_required_sources: set[SourceIdentity],
        rejected_desired_records: dict[SourceIdentity, TargetRecord],
    ) -> None:
        """Run serialized catalog-owned deltas until this session ends."""
        while True:
            await dirty_signal.event.wait()
            await asyncio.sleep(CONTINUOUS_COALESCE_SECONDS)
            cycle_generation = dirty_signal.generation
            dirty_signal.event.clear()

            reports = await self._async_reconcile_live_snapshot(
                session,
                instance_id=instance_id,
                label_id=label_id,
                strategies=strategies,
                included_kinds=included_kinds,
                blocked_sources=blocked_sources,
                capacity_blocked_sources=capacity_blocked_sources,
                active_capacity_blocked_durable_sources=(
                    active_capacity_blocked_durable_sources
                ),
                fresh_inventory_required_sources=fresh_inventory_required_sources,
                rejected_desired_records=rejected_desired_records,
            )
            self._log_reports(reports)

            if dirty_signal.generation != cycle_generation:
                dirty_signal.event.set()

    async def _async_reconcile_live_snapshot(
        self,
        session: BridgeApplicationSession,
        *,
        instance_id: str,
        label_id: str,
        strategies: tuple[_ExportKindStrategy, ...],
        included_kinds: frozenset[CapabilityKind],
        blocked_sources: set[SourceIdentity],
        capacity_blocked_sources: set[SourceIdentity],
        active_capacity_blocked_durable_sources: set[SourceIdentity],
        fresh_inventory_required_sources: set[SourceIdentity],
        rejected_desired_records: dict[SourceIdentity, TargetRecord],
    ) -> list[ExecutionReport]:
        """Collect and apply one jointly preflighted catalog-owned live delta."""
        collection = collect_export_selection(
            self._hass,
            instance_id=instance_id,
            label_id=label_id,
            included_kinds=included_kinds,
        )
        self._report_exclusions(session, collection.exclusions)
        staged_storages, catalogs = await self._async_preload_catalogs(
            session,
            collection.capabilities,
        )

        current_sources = {capability.source for capability in collection.capabilities}
        catalog_sources = {
            record.capability.source
            for catalog in catalogs.values()
            for record in catalog.records
        }
        fresh_inventory_required_sources.update(
            active_capacity_blocked_durable_sources - current_sources
        )
        active_capacity_blocked_durable_sources.intersection_update(current_sources)
        if current_sources & fresh_inventory_required_sources:
            raise ConnectionError("fresh inventory is required")
        blocked_sources.intersection_update(current_sources)
        if current_sources - catalog_sources - blocked_sources:
            raise ConnectionError("fresh inventory is required")

        reports: list[ExecutionReport] = []
        for strategy in strategies:
            report = await self._async_reconcile_live_kind(
                session,
                instance_id,
                collection.capabilities,
                strategy=strategy,
                catalog=catalogs[strategy.kind],
                storage=staged_storages[strategy.kind],
                capacity_blocked_sources=capacity_blocked_sources,
                rejected_desired_records=rejected_desired_records,
            )
            self._ensure_persistence_confirmed(report)
            reports.append(report)
        return reports

    @staticmethod
    def _ensure_persistence_confirmed(report: ExecutionReport) -> None:
        """Stop before another catalog is touched after an uncertain write."""
        if report.persistence_uncertain:
            _LOGGER.warning(
                "Domoticz export reconciliation stopped because catalog "
                "persistence could not be confirmed"
            )
            raise ProtocolError("export reconciliation is unavailable")

    async def _async_reconcile_kind(
        self,
        session: BridgeApplicationSession,
        instance_id: str,
        capabilities: tuple[Capability, ...],
        *,
        strategy: _ExportKindStrategy,
        observations: tuple[TargetObservation, ...] | None = None,
        storage: CatalogStorage | None = None,
    ) -> ExecutionReport:
        """Reconcile one negotiated kind in its independent target catalog."""
        adapter = strategy.adapter_factory(session)
        if storage is None:
            storage = self._storage_for_strategy(session, strategy)
        executor = ReconciliationExecutor(adapter, storage)
        scope = SourceScope(_SOURCE_SYSTEM, instance_id)
        current = tuple(
            capability
            for capability in capabilities
            if capability.kind is strategy.kind
        )
        if observations is None:
            return await executor.async_reconcile(scope, current)
        return await executor.async_reconcile(scope, current, observations)

    async def _async_reconcile_live_kind(
        self,
        session: BridgeApplicationSession,
        instance_id: str,
        capabilities: tuple[Capability, ...],
        *,
        strategy: _ExportKindStrategy,
        catalog: TargetCatalog,
        storage: CatalogStorage,
        capacity_blocked_sources: set[SourceIdentity],
        rejected_desired_records: dict[SourceIdentity, TargetRecord],
    ) -> ExecutionReport:
        """Apply only changed records that this session's catalogs already own."""
        adapter = strategy.adapter_factory(session)

        scope = SourceScope(_SOURCE_SYSTEM, instance_id)
        current = tuple(
            capability
            for capability in capabilities
            if capability.kind is strategy.kind
        )
        planned = plan_reconciliation(scope, current, catalog.records)
        actions = tuple(
            action
            for action in planned
            if action.kind is not ReconciliationActionKind.CREATE
            and action.capability.source not in capacity_blocked_sources
            and not _action_matches_catalog(action, catalog)
        )
        actions = _suppress_unchanged_rejections(
            strategy.kind,
            actions,
            rejected_desired_records,
        )
        report = await async_execute_reconciliation(
            catalog,
            actions,
            adapter,
            storage,
        )
        _update_rejected_desired_records(rejected_desired_records, [report])
        return report

    async def _async_preload_catalogs(
        self,
        session: BridgeApplicationSession,
        capabilities: tuple[Capability, ...],
    ) -> tuple[
        dict[CapabilityKind, CatalogStorage],
        dict[CapabilityKind, TargetCatalog],
    ]:
        """Load and jointly validate all catalogs before the first target write."""
        storages = tuple(
            self._storage_for_strategy(session, strategy)
            for strategy in _ALL_CATALOG_STRATEGIES
        )
        documents = await asyncio.gather(
            *(storage.async_load() for storage in storages)
        )
        catalogs = tuple(
            TargetCatalog() if document is None else catalog_from_document(document)
            for document in documents
        )
        validate_deterministic_target_ownership(
            capabilities,
            (record for catalog in catalogs for record in catalog.records),
        )
        return (
            {
                strategy.kind: _PreloadedCatalogStorage(storage, document)
                for strategy, storage, document in zip(
                    _ALL_CATALOG_STRATEGIES,
                    storages,
                    documents,
                    strict=True,
                )
            },
            {
                strategy.kind: catalog
                for strategy, catalog in zip(
                    _ALL_CATALOG_STRATEGIES,
                    catalogs,
                    strict=True,
                )
            },
        )

    def _storage_for_strategy(
        self,
        session: BridgeApplicationSession,
        strategy: _ExportKindStrategy,
    ) -> CatalogStorage:
        """Build one destination-scoped catalog adapter for a capability kind."""
        return strategy.storage_factory(
            self._hass,
            entry_id=session.entry_id,
            destination_id=session.destination_id,
        )

    def _report_exclusions(
        self,
        session: BridgeApplicationSession,
        exclusions: tuple[ExportExclusion, ...],
    ) -> None:
        """Warn once for each current safe exclusion diagnostic."""
        key = (session.entry_id, session.destination_id)
        current = frozenset(exclusions)
        previous = self._reported_exclusions.get(key, frozenset())
        for exclusion in sorted(
            current - previous,
            key=lambda item: (item.entity_id, item.reason.value),
        ):
            _LOGGER.warning(
                "Domoticz export skipped directly labelled entity %s: %s",
                exclusion.entity_id,
                exclusion.reason.value,
            )
        self._reported_exclusions[key] = current


def _admit_inventory_creates(
    scope: SourceScope,
    capabilities: tuple[Capability, ...],
    observations: tuple[TargetObservation, ...],
    catalogs: Mapping[CapabilityKind, TargetCatalog],
    included_kinds: frozenset[CapabilityKind],
) -> _InventoryAdmission:
    """Reserve durable recovery first, then admit globally ordered new targets."""
    observations_by_target_id: dict[str, TargetObservation] = {}
    for observation in observations:
        if observation.target_id in observations_by_target_id:
            raise TargetBindingError("duplicate observed target identity")
        observations_by_target_id[observation.target_id] = observation

    reserved_target_ids = set(observations_by_target_id)
    units_used = sum(
        len(observation.units) for observation in observations_by_target_id.values()
    )
    if (
        len(reserved_target_ids) > MAX_INVENTORY_TARGETS
        or units_used > MAX_INVENTORY_UNITS
    ):
        raise TargetBindingError("target inventory capacity is unavailable")

    current_sources = {capability.source for capability in capabilities}
    durable_records = tuple(
        record for catalog in catalogs.values() for record in catalog.records
    )
    recovery_reservations = tuple(
        record
        for record in durable_records
        if (
            observations_by_target_id.get(record.target_id) is None
            or observations_by_target_id[record.target_id].units == ()
        )
    )
    blocked_durable_sources: set[SourceIdentity] = set()
    for record in sorted(
        recovery_reservations,
        key=lambda item: (
            item.capability.source not in current_sources,
            item.capability.source.key,
        ),
    ):
        source = record.capability.source
        target_cost = int(record.target_id not in reserved_target_ids)
        if (
            len(reserved_target_ids) + target_cost > MAX_INVENTORY_TARGETS
            or units_used + 1 > MAX_INVENTORY_UNITS
        ):
            blocked_durable_sources.add(source)
            continue
        reserved_target_ids.add(record.target_id)
        units_used += 1

    create_actions = []
    for kind in sorted(included_kinds, key=lambda item: item.value):
        current = tuple(
            capability for capability in capabilities if capability.kind is kind
        )
        create_actions.extend(
            action
            for action in plan_reconciliation(
                scope,
                current,
                catalogs[kind].records,
                observations,
            )
            if action.kind is ReconciliationActionKind.CREATE
        )

    blocked_create_sources: set[SourceIdentity] = set()
    for action in sorted(
        create_actions,
        key=lambda item: item.capability.source.key,
    ):
        source = action.capability.source
        target_id = derive_domoticz_target_id(source)
        target_cost = int(target_id not in reserved_target_ids)
        if (
            len(reserved_target_ids) + target_cost > MAX_INVENTORY_TARGETS
            or units_used + 1 > MAX_INVENTORY_UNITS
        ):
            blocked_create_sources.add(source)
            continue
        reserved_target_ids.add(target_id)
        units_used += 1

    blocked_sources = blocked_durable_sources | blocked_create_sources
    admitted = tuple(
        capability
        for capability in capabilities
        if capability.source not in blocked_sources
    )
    return _InventoryAdmission(
        capabilities=admitted,
        blocked_sources=frozenset(blocked_sources),
        blocked_durable_sources=frozenset(blocked_durable_sources),
    )


def _desired_record(action: ReconciliationAction) -> TargetRecord:
    """Normalize CREATE and existing-target actions to one desired target state."""
    target_id = action.target_id
    if target_id is None:
        target_id = derive_domoticz_target_id(action.capability.source)
    return TargetRecord(
        target_id=target_id,
        capability=action.capability,
        stale=action.stale,
    )


def _suppress_unchanged_rejections(
    kind: CapabilityKind,
    actions: tuple[ReconciliationAction, ...],
    rejected_desired_records: dict[SourceIdentity, TargetRecord],
) -> tuple[ReconciliationAction, ...]:
    """Skip only the same desired state rejected earlier in this session."""
    desired_by_source = {
        action.capability.source: _desired_record(action) for action in actions
    }
    for source, rejected in tuple(rejected_desired_records.items()):
        if rejected.capability.kind is not kind:
            continue
        if desired_by_source.get(source) != rejected:
            rejected_desired_records.pop(source)

    return tuple(
        action
        for action in actions
        if rejected_desired_records.get(action.capability.source)
        != desired_by_source[action.capability.source]
    )


def _update_rejected_desired_records(
    rejected_desired_records: dict[SourceIdentity, TargetRecord],
    reports: list[ExecutionReport],
) -> None:
    """Remember expected rejections without leaking them across sessions."""
    for report in reports:
        for result in report.results:
            source = result.action.capability.source
            if result.status is ExecutionStatus.TARGET_NOT_CONFIRMED:
                rejected_desired_records[source] = _desired_record(result.action)
            elif result.status is ExecutionStatus.COMMITTED:
                rejected_desired_records.pop(source, None)


def _action_matches_catalog(
    action: ReconciliationAction,
    catalog: TargetCatalog,
) -> bool:
    """Return whether an existing record already represents one live action."""
    if action.kind is ReconciliationActionKind.CREATE:
        return False
    existing = catalog.get(action.capability.source)
    if existing is None:
        return False
    return existing == _desired_record(action)


async def _async_fetch_inventory(
    session: BridgeApplicationSession,
) -> tuple[InventoryTarget, ...]:
    """Request and fully stage one bounded inventory before reconciliation."""
    request_id = generate_request_id()
    pages: list[InventoryResult] = []
    target_count = 0
    unit_count = 0
    previous_target_id: str | None = None

    async with asyncio.timeout(INVENTORY_TIMEOUT):
        await session.async_send(build_inventory_request(session.selection, request_id))
        while True:
            payload = await session.async_receive()
            if isinstance(payload, dict) and payload.get("type") == "ping":
                ping_id = _parse_ping(payload)
                await session.async_send({"id": ping_id, "type": "pong"})
                continue

            result = parse_inventory_result(session.selection, payload)
            expected_page = len(pages) + 1
            if (
                expected_page > MAX_INVENTORY_PAGES
                or result.request_id != request_id
                or result.page != expected_page
            ):
                raise ProtocolFormatError("invalid protocol message")

            for target in result.targets:
                if (
                    previous_target_id is not None
                    and target.target_id <= previous_target_id
                ):
                    raise ProtocolFormatError("invalid protocol message")
                previous_target_id = target.target_id
                target_count += 1
                unit_count += len(target.units)
                if (
                    target_count > MAX_INVENTORY_TARGETS
                    or unit_count > MAX_INVENTORY_UNITS
                ):
                    raise ProtocolFormatError("invalid protocol message")

            pages.append(result)
            if result.complete:
                return assemble_inventory_results(
                    session.selection,
                    request_id,
                    pages,
                )


def _parse_ping(payload: dict[str, object]) -> str:
    """Parse the bridge's exact heartbeat request shape."""
    if set(payload) != {"id", "type"} or payload["type"] != "ping":
        raise ProtocolError("invalid protocol message")
    ping_id = payload["id"]
    validate_nonce(ping_id)
    assert isinstance(ping_id, str)
    return ping_id
