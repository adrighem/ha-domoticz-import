"""Connect-time Home Assistant export reconciliation for the Domoticz bridge."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .catalog_storage import HomeAssistantCatalogStorage
from .const import CONF_EXPORT_LABEL_ID
from .core.capabilities import CapabilityKind
from .core.catalog import CatalogFormatError
from .core.execution import (
    ApplyConfirmation,
    CatalogStorageError,
    ExecutionConflictError,
    ExecutionStatus,
    ReconciliationExecutor,
    TargetActionError,
)
from .core.protocol import (
    ApplyResultStatus,
    ProtocolError,
    build_apply,
    generate_request_id,
    parse_apply_result,
    validate_nonce,
)
from .core.reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
    SourceScope,
)
from .home_assistant_source import (
    ExportLabelNotFoundError,
    collect_export_capabilities,
)

if TYPE_CHECKING:
    from .bridge import BridgeApplicationSession

_LOGGER = logging.getLogger(__name__)

APPLY_TIMEOUT = 10.0
_SOURCE_SYSTEM = "home_assistant"


class DomoticzSessionTargetAdapter:
    """Apply reconciliation actions over one authenticated bridge session."""

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
            await self._session.async_send(build_apply(request_id, action))
            while True:
                payload = await self._session.async_receive()
                if isinstance(payload, dict) and payload.get("type") == "ping":
                    ping_id = _parse_ping(payload)
                    await self._session.async_send({"id": ping_id, "type": "pong"})
                    continue

                result = parse_apply_result(payload)
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


class HomeAssistantExportApplication:
    """Reconcile labelled numeric entities when a bridge session connects."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Store the Home Assistant instance used for source collection."""
        self._hass = hass

    async def async_connected(self, session: BridgeApplicationSession) -> None:
        """Run one fail-closed numeric reconciliation for a ready session."""
        entry = self._hass.config_entries.async_get_entry(session.entry_id)
        if entry is None:
            raise ProtocolError("export reconciliation is unavailable")

        label_id = entry.data.get(CONF_EXPORT_LABEL_ID)
        if not isinstance(label_id, str) or not label_id:
            raise ProtocolError("export reconciliation is unavailable")

        try:
            instance_id = await async_get_instance_id(self._hass)
            current = tuple(
                capability
                for capability in collect_export_capabilities(
                    self._hass,
                    instance_id=instance_id,
                    label_id=label_id,
                )
                if capability.kind is CapabilityKind.NUMERIC
            )
            executor = ReconciliationExecutor(
                DomoticzSessionTargetAdapter(session),
                HomeAssistantCatalogStorage(
                    self._hass,
                    entry_id=session.entry_id,
                    destination_id=session.destination_id,
                ),
            )
            report = await executor.async_reconcile(
                SourceScope(_SOURCE_SYSTEM, instance_id),
                current,
            )
        except (
            CatalogFormatError,
            CatalogStorageError,
            ExecutionConflictError,
            ExportLabelNotFoundError,
        ) as error:
            raise ProtocolError("export reconciliation is unavailable") from error

        if report.persistence_uncertain:
            _LOGGER.warning(
                "Domoticz export reconciliation stopped because catalog "
                "persistence could not be confirmed"
            )
            raise ProtocolError("export reconciliation is unavailable")

        committed = sum(
            result.status is ExecutionStatus.COMMITTED for result in report.results
        )
        rejected = sum(
            result.status is ExecutionStatus.TARGET_NOT_CONFIRMED
            for result in report.results
        )
        _LOGGER.info(
            "Domoticz export reconciliation completed: "
            "%d planned, %d committed, %d rejected",
            len(report.actions),
            committed,
            rejected,
        )


def _parse_ping(payload: dict[str, object]) -> str:
    """Parse the bridge's exact heartbeat request shape."""
    if set(payload) != {"id", "type"} or payload["type"] != "ping":
        raise ProtocolError("invalid protocol message")
    ping_id = payload["id"]
    validate_nonce(ping_id)
    assert isinstance(ping_id, str)
    return ping_id
