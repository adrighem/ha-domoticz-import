"""Tests for connect-time Home Assistant export reconciliation."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from custom_components.domoticz_sync import (
    bridge_reconciliation as app_module,  # noqa: E402
)
from custom_components.domoticz_sync.bridge_reconciliation import (  # noqa: E402
    DomoticzSessionTargetAdapter,
    HomeAssistantExportApplication,
)
from custom_components.domoticz_sync.const import (  # noqa: E402
    CONF_EXPORT_LABEL_ID,
)
from custom_components.domoticz_sync.core import (
    protocol as protocol_module,  # noqa: E402
)
from custom_components.domoticz_sync.core.capabilities import (  # noqa: E402
    Capability,
    CapabilityKind,
    SourceIdentity,
)
from custom_components.domoticz_sync.core.catalog import (  # noqa: E402
    catalog_from_document,
)
from custom_components.domoticz_sync.core.execution import (  # noqa: E402
    TargetActionError,
)
from custom_components.domoticz_sync.core.protocol import (  # noqa: E402
    ApplyResultStatus,
    ProtocolError,
    build_apply_result,
    generate_nonce,
    parse_apply,
)
from custom_components.domoticz_sync.core.reconciliation import (  # noqa: E402
    ReconciliationAction,
    ReconciliationActionKind,
)


class _ConfigEntries:
    """Minimal config-entry lookup used by the application."""

    def __init__(self, entry_id: str = "entry-1") -> None:
        self._entry_id = entry_id
        self._entry = SimpleNamespace(data={CONF_EXPORT_LABEL_ID: "export-label"})

    def async_get_entry(self, entry_id: str):
        """Return the configured entry by its stable ID."""
        return self._entry if entry_id == self._entry_id else None


class _Hass:
    """Minimal Home Assistant shape required by the application."""

    def __init__(self) -> None:
        self.config_entries = _ConfigEntries()


class _MemoryStorage:
    """In-memory catalog storage for application tests."""

    def __init__(self) -> None:
        self.document = None
        self.saved_documents: list[dict[str, object]] = []

    async def async_load(self):
        """Return an isolated durable document."""
        return deepcopy(self.document)

    async def async_save(self, document):
        """Replace the durable document."""
        self.document = deepcopy(document)
        self.saved_documents.append(deepcopy(document))


class _Session:
    """Sequential bridge application session with scripted responses."""

    entry_id = "entry-1"
    destination_id = "destination-1"

    def __init__(self, response_builder=None) -> None:
        self.sent: list[dict[str, object]] = []
        self._responses: deque[dict[str, object]] = deque()
        self._response_builder = response_builder
        self._never_respond = asyncio.Event()

    async def async_send(self, payload: dict[str, object]) -> None:
        """Record a payload and enqueue responses to apply requests."""
        self.sent.append(deepcopy(payload))
        if payload.get("type") == "apply" and self._response_builder is not None:
            self._responses.extend(self._response_builder(payload))

    async def async_receive(self) -> dict[str, object]:
        """Return the next scripted payload or remain pending."""
        if self._responses:
            return self._responses.popleft()
        await self._never_respond.wait()
        raise AssertionError("unreachable")


def _source(object_id: str) -> SourceIdentity:
    """Build one stable Home Assistant source identity."""
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
) -> Capability:
    """Build one available source capability."""
    return Capability(
        source=_source(object_id),
        kind=kind,
        name=object_id,
        value=12.5 if kind is CapabilityKind.NUMERIC else True,
        unit="celsius" if kind is CapabilityKind.NUMERIC else None,
    )


def _create_action(object_id: str = "sensor-a") -> ReconciliationAction:
    """Build one numeric create action."""
    return ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_capability(object_id),
    )


def _confirmed_response(payload: dict[str, object]) -> list[dict[str, object]]:
    """Confirm one apply request using its exact source."""
    request = parse_apply(payload)
    return [
        build_apply_result(
            request.request_id,
            ApplyResultStatus.CONFIRMED,
            f"target-{request.action.capability.source.object_id}",
            request.action.capability.source,
        )
    ]


def _configure_application(
    monkeypatch: pytest.MonkeyPatch,
    capabilities: list[Capability],
    storage: _MemoryStorage,
) -> None:
    """Inject deterministic source collection and catalog storage."""
    monkeypatch.setattr(
        app_module,
        "async_get_instance_id",
        AsyncMock(return_value="instance-1"),
    )

    def collect(_hass, *, instance_id: str, label_id: str):
        assert instance_id == "instance-1"
        assert label_id == "export-label"
        return capabilities

    monkeypatch.setattr(app_module, "collect_export_capabilities", collect)

    def make_storage(_hass, *, entry_id: str, destination_id: str):
        assert entry_id == "entry-1"
        assert destination_id == "destination-1"
        return storage

    monkeypatch.setattr(app_module, "HomeAssistantCatalogStorage", make_storage)


@pytest.mark.asyncio
async def test_application_filters_binary_and_commits_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only numeric capabilities are applied and committed."""
    numeric = _capability("numeric-source")
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    storage = _MemoryStorage()
    _configure_application(monkeypatch, [binary, numeric], storage)
    session = _Session(_confirmed_response)

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    requests = [
        parse_apply(payload)
        for payload in session.sent
        if payload.get("type") == "apply"
    ]
    assert [request.action.capability for request in requests] == [numeric]
    catalog = catalog_from_document(storage.document)
    assert len(catalog.records) == 1
    assert catalog.records[0].capability == numeric
    assert len(storage.saved_documents) == 1


@pytest.mark.asyncio
async def test_rejected_action_continues_without_catalog_mutation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One rejection does not block or commit unrelated source state."""
    rejected = _capability("private-rejected-source")
    confirmed = _capability("private-confirmed-source")
    storage = _MemoryStorage()
    _configure_application(monkeypatch, [rejected, confirmed], storage)

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        request = parse_apply(payload)
        if request.action.capability.source == rejected.source:
            return [
                build_apply_result(
                    request.request_id,
                    ApplyResultStatus.REJECTED,
                    None,
                    None,
                )
            ]
        return _confirmed_response(payload)

    session = _Session(responses)

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    requests = [
        parse_apply(payload)
        for payload in session.sent
        if payload.get("type") == "apply"
    ]
    assert len(requests) == 2
    catalog = catalog_from_document(storage.document)
    assert catalog.get(rejected.source) is None
    assert catalog.get(confirmed.source) is not None
    assert len(storage.saved_documents) == 1
    assert "private-rejected-source" not in caplog.text
    assert "private-confirmed-source" not in caplog.text


@pytest.mark.asyncio
async def test_adapter_rejects_result_for_different_request() -> None:
    """A result cannot satisfy a different in-flight request."""
    action = _create_action()

    def responses(_payload: dict[str, object]) -> list[dict[str, object]]:
        return [
            build_apply_result(
                "request-different",
                ApplyResultStatus.CONFIRMED,
                "target-a",
                action.capability.source,
            )
        ]

    with pytest.raises(ProtocolError, match="invalid protocol message"):
        await DomoticzSessionTargetAdapter(_Session(responses)).async_apply(action)


@pytest.mark.asyncio
async def test_adapter_rejects_confirmation_for_different_source() -> None:
    """A confirmation must carry the action's exact source identity."""
    action = _create_action()

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        request = parse_apply(payload)
        return [
            build_apply_result(
                request.request_id,
                ApplyResultStatus.CONFIRMED,
                "target-a",
                _source("different-source"),
            )
        ]

    with pytest.raises(ProtocolError, match="invalid protocol message"):
        await DomoticzSessionTargetAdapter(_Session(responses)).async_apply(action)


@pytest.mark.asyncio
async def test_adapter_propagates_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing result times out so the bridge can close and retry."""
    monkeypatch.setattr(app_module, "APPLY_TIMEOUT", 0.01)

    with pytest.raises(TimeoutError):
        await DomoticzSessionTargetAdapter(_Session()).async_apply(_create_action())


@pytest.mark.asyncio
async def test_adapter_bounds_a_blocked_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The action deadline also covers transport backpressure while sending."""

    class _BlockedSendSession(_Session):
        async def async_send(self, payload: dict[str, object]) -> None:
            del payload
            await self._never_respond.wait()

    monkeypatch.setattr(app_module, "APPLY_TIMEOUT", 0.01)

    with pytest.raises(TimeoutError):
        await DomoticzSessionTargetAdapter(_BlockedSendSession()).async_apply(
            _create_action()
        )


@pytest.mark.asyncio
async def test_adapter_answers_interleaved_ping_before_confirmation() -> None:
    """Heartbeat traffic remains valid while an action is in flight."""
    ping_id = generate_nonce()

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        return [
            {"id": ping_id, "type": "ping"},
            *_confirmed_response(payload),
        ]

    session = _Session(responses)
    action = _create_action()

    confirmation = await DomoticzSessionTargetAdapter(session).async_apply(action)

    assert confirmation.source == action.capability.source
    assert session.sent[1] == {"id": ping_id, "type": "pong"}


@pytest.mark.parametrize(
    ("first_byte", "raw_prefix"),
    ((0xF8, "-"), (0xFC, "_")),
)
@pytest.mark.asyncio
async def test_adapter_generates_valid_ids_for_problematic_raw_tokens(
    monkeypatch: pytest.MonkeyPatch,
    first_byte: int,
    raw_prefix: str,
) -> None:
    """The adapter uses the request-specific generator for every apply."""
    random_bytes = bytes([first_byte]) + bytes(31)
    monkeypatch.setattr(
        protocol_module.secrets,
        "token_bytes",
        lambda size: random_bytes if size == 32 else bytes(size),
    )
    session = _Session(_confirmed_response)

    await DomoticzSessionTargetAdapter(session).async_apply(_create_action())

    request_id = parse_apply(session.sent[0]).request_id
    assert request_id.startswith(f"request_{raw_prefix}")


@pytest.mark.asyncio
async def test_adapter_rejects_non_strict_interleaved_ping() -> None:
    """Heartbeat messages cannot carry additional fields."""
    ping_id = generate_nonce()

    def responses(_payload: dict[str, object]) -> list[dict[str, object]]:
        return [{"extra": True, "id": ping_id, "type": "ping"}]

    with pytest.raises(ProtocolError, match="invalid protocol message"):
        await DomoticzSessionTargetAdapter(_Session(responses)).async_apply(
            _create_action()
        )


@pytest.mark.asyncio
async def test_adapter_turns_rejection_into_isolated_target_error() -> None:
    """A strict remote rejection remains local to its action."""

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        request = parse_apply(payload)
        return [
            build_apply_result(
                request.request_id,
                ApplyResultStatus.REJECTED,
                None,
                None,
            )
        ]

    with pytest.raises(TargetActionError, match="target action was rejected"):
        await DomoticzSessionTargetAdapter(_Session(responses)).async_apply(
            _create_action()
        )
