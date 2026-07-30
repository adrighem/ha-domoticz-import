"""Connect-time Home Assistant export reconciliation for the Domoticz bridge."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .catalog_storage import (
    HomeAssistantBinaryCatalogStorage,
    HomeAssistantCatalogStorage,
)
from .const import CONF_EXPORT_LABEL_ID
from .core.capabilities import Capability, CapabilityKind
from .core.catalog import CatalogFormatError
from .core.execution import (
    ApplyConfirmation,
    CatalogStorageError,
    ExecutionConflictError,
    ExecutionReport,
    ExecutionStatus,
    ReconciliationExecutor,
    TargetActionError,
)
from .core.protocol import (
    FEATURE_HA_EXPORT_BINARY_V1,
    FEATURE_HA_EXPORT_NUMERIC_V1,
    ApplyResult,
    ApplyResultStatus,
    ProtocolError,
    build_apply,
    build_binary_apply,
    generate_request_id,
    parse_apply_result,
    parse_binary_apply_result,
    validate_nonce,
)
from .core.reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
    SourceScope,
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
_SOURCE_SYSTEM = "home_assistant"


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
            self._report_exclusions(session, collection.exclusions)
            reports: list[ExecutionReport] = []
            if numeric_enabled:
                report = await self._async_reconcile_kind(
                    session,
                    instance_id,
                    collection.capabilities,
                    kind=CapabilityKind.NUMERIC,
                )
                self._ensure_persistence_confirmed(report)
                reports.append(report)
            if binary_enabled:
                report = await self._async_reconcile_kind(
                    session,
                    instance_id,
                    collection.capabilities,
                    kind=CapabilityKind.BINARY,
                )
                self._ensure_persistence_confirmed(report)
                reports.append(report)
        except (
            CatalogFormatError,
            CatalogStorageError,
            ExecutionConflictError,
            ExportLabelNotFoundError,
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
    ) -> ExecutionReport:
        """Reconcile one negotiated kind in its independent target catalog."""
        if kind is CapabilityKind.NUMERIC:
            adapter = DomoticzSessionTargetAdapter(session)
            storage = HomeAssistantCatalogStorage(
                self._hass,
                entry_id=session.entry_id,
                destination_id=session.destination_id,
            )
        elif kind is CapabilityKind.BINARY:
            adapter = DomoticzBinarySessionTargetAdapter(session)
            storage = HomeAssistantBinaryCatalogStorage(
                self._hass,
                entry_id=session.entry_id,
                destination_id=session.destination_id,
            )
        else:
            raise ValueError("unsupported export capability kind")
        executor = ReconciliationExecutor(adapter, storage)
        return await executor.async_reconcile(
            SourceScope(_SOURCE_SYSTEM, instance_id),
            (capability for capability in capabilities if capability.kind is kind),
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


def _parse_ping(payload: dict[str, object]) -> str:
    """Parse the bridge's exact heartbeat request shape."""
    if set(payload) != {"id", "type"} or payload["type"] != "ping":
        raise ProtocolError("invalid protocol message")
    ping_id = payload["id"]
    validate_nonce(ping_id)
    assert isinstance(ping_id, str)
    return ping_id
