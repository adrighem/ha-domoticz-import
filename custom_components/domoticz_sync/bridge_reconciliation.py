"""Connect-time Home Assistant export reconciliation for the Domoticz bridge."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .catalog_storage import (
    HomeAssistantBinaryCatalogStorage,
    HomeAssistantCatalogStorage,
)
from .const import CONF_EXPORT_LABEL_ID
from .core.capabilities import Capability, CapabilityKind
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
)
from .core.protocol import (
    FEATURE_DOMOTICZ_INVENTORY_V1,
    FEATURE_HA_EXPORT_BINARY_V1,
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
    validate_deterministic_target_ownership,
)
from .home_assistant_source import (
    ExportExclusion,
    ExportLabelNotFoundError,
    collect_export_selection,
)

if TYPE_CHECKING:
    from .bridge import BridgeApplicationSession

_LOGGER = logging.getLogger(__name__)

APPLY_TIMEOUT = 10.0
INVENTORY_TIMEOUT = float(INVENTORY_TIMEOUT_SECONDS)
_SOURCE_SYSTEM = "home_assistant"


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


class HomeAssistantExportApplication:
    """Reconcile negotiated labelled entities when a bridge session connects."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the Home Assistant instance used for source collection."""
        self._hass = hass
        self._reported_exclusions: dict[
            tuple[str, str], frozenset[ExportExclusion]
        ] = {}

    async def async_connected(self, session: BridgeApplicationSession) -> None:
        """Run one fail-closed reconciliation for each negotiated capability kind."""
        numeric_enabled = session.supports(FEATURE_HA_EXPORT_NUMERIC_V1)
        binary_enabled = session.supports(FEATURE_HA_EXPORT_BINARY_V1)
        if not numeric_enabled and not binary_enabled:
            return

        entry = self._hass.config_entries.async_get_entry(session.entry_id)
        if entry is None:
            raise ProtocolError("export reconciliation is unavailable")

        label_id = entry.data.get(CONF_EXPORT_LABEL_ID)
        if not isinstance(label_id, str) or not label_id:
            raise ProtocolError("export reconciliation is unavailable")

        try:
            instance_id = await async_get_instance_id(self._hass)
            included_kinds = set()
            if numeric_enabled:
                included_kinds.add(CapabilityKind.NUMERIC)
            if binary_enabled:
                included_kinds.add(CapabilityKind.BINARY)
            collection = collect_export_selection(
                self._hass,
                instance_id=instance_id,
                label_id=label_id,
                included_kinds=frozenset(included_kinds),
            )
            observations: tuple[TargetObservation, ...] | None = None
            staged_storages: dict[CapabilityKind, CatalogStorage] = {}
            if session.supports(FEATURE_DOMOTICZ_INVENTORY_V1):
                inventory = await _async_fetch_inventory(session)
                observations = tuple(
                    TargetObservation(
                        target.target_id,
                        tuple(unit.unit for unit in target.units),
                    )
                    for target in inventory
                )
                staged_storages = await self._async_preload_catalogs(
                    session,
                    collection.capabilities,
                )
            self._report_exclusions(session, collection.exclusions)

            reports: list[ExecutionReport] = []
            if numeric_enabled:
                report = await self._async_reconcile_kind(
                    session,
                    instance_id,
                    collection.capabilities,
                    kind=CapabilityKind.NUMERIC,
                    observations=observations,
                    storage=staged_storages.get(CapabilityKind.NUMERIC),
                )
                self._ensure_persistence_confirmed(report)
                reports.append(report)
            if binary_enabled:
                report = await self._async_reconcile_kind(
                    session,
                    instance_id,
                    collection.capabilities,
                    kind=CapabilityKind.BINARY,
                    observations=observations,
                    storage=staged_storages.get(CapabilityKind.BINARY),
                )
                self._ensure_persistence_confirmed(report)
                reports.append(report)
        except (
            CatalogFormatError,
            CatalogStorageError,
            ExecutionConflictError,
            ExportLabelNotFoundError,
            TargetBindingError,
        ) as error:
            raise ProtocolError("export reconciliation is unavailable") from error

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
        kind: CapabilityKind,
        observations: tuple[TargetObservation, ...] | None = None,
        storage: CatalogStorage | None = None,
    ) -> ExecutionReport:
        """Reconcile one negotiated kind in its independent target catalog."""
        if kind is CapabilityKind.NUMERIC:
            adapter = DomoticzSessionTargetAdapter(session)
        elif kind is CapabilityKind.BINARY:
            adapter = DomoticzBinarySessionTargetAdapter(session)
        else:
            raise ValueError("unsupported export capability kind")
        if storage is None:
            storage = self._storage_for_kind(session, kind)
        executor = ReconciliationExecutor(adapter, storage)
        scope = SourceScope(_SOURCE_SYSTEM, instance_id)
        current = tuple(
            capability for capability in capabilities if capability.kind is kind
        )
        if observations is None:
            return await executor.async_reconcile(scope, current)
        return await executor.async_reconcile(scope, current, observations)

    async def _async_preload_catalogs(
        self,
        session: BridgeApplicationSession,
        capabilities: tuple[Capability, ...],
    ) -> dict[CapabilityKind, CatalogStorage]:
        """Load and jointly validate all catalogs before the first target write."""
        ordered_kinds = (CapabilityKind.BINARY, CapabilityKind.NUMERIC)
        storages = tuple(
            self._storage_for_kind(session, kind) for kind in ordered_kinds
        )
        documents = await asyncio.gather(
            *(storage.async_load() for storage in storages)
        )
        catalogs = tuple(
            TargetCatalog()
            if document is None
            else catalog_from_document(document)
            for document in documents
        )
        validate_deterministic_target_ownership(
            capabilities,
            (record for catalog in catalogs for record in catalog.records),
        )
        return {
            kind: _PreloadedCatalogStorage(storage, document)
            for kind, storage, document in zip(
                ordered_kinds,
                storages,
                documents,
                strict=True,
            )
        }

    def _storage_for_kind(
        self,
        session: BridgeApplicationSession,
        kind: CapabilityKind,
    ) -> CatalogStorage:
        """Build one destination-scoped catalog adapter for a capability kind."""
        if kind is CapabilityKind.NUMERIC:
            storage_type = HomeAssistantCatalogStorage
        elif kind is CapabilityKind.BINARY:
            storage_type = HomeAssistantBinaryCatalogStorage
        else:
            raise ValueError("unsupported export capability kind")
        return storage_type(
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
        await session.async_send(
            build_inventory_request(session.selection, request_id)
        )
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
