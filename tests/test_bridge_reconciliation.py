"""Tests for connect-time Home Assistant export reconciliation."""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("homeassistant")

from custom_components.domoticz_sync import (
    bridge_reconciliation as app_module,  # noqa: E402
)
from custom_components.domoticz_sync.bridge_reconciliation import (  # noqa: E402
    DomoticzBinarySessionTargetAdapter,
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
    Availability,
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
    FEATURE_HA_EXPORT_BINARY_V1,
    FEATURE_HA_EXPORT_NUMERIC_V1,
    PROTOCOL_VERSION_V2,
    WEBSOCKET_SUBPROTOCOL_V2,
    ApplyResultStatus,
    ProtocolError,
    ProtocolSelection,
    build_apply_result,
    build_binary_apply_result,
    generate_nonce,
    parse_apply,
    parse_binary_apply,
)
from custom_components.domoticz_sync.core.reconciliation import (  # noqa: E402
    ReconciliationAction,
    ReconciliationActionKind,
)
from custom_components.domoticz_sync.home_assistant_source import (  # noqa: E402
    ExportCollection,
    ExportExclusion,
    ExportExclusionReason,
)

_SELECTION = ProtocolSelection(
    version=PROTOCOL_VERSION_V2,
    websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
    features=(FEATURE_HA_EXPORT_NUMERIC_V1,),
)
_BINARY_SELECTION = ProtocolSelection(
    version=PROTOCOL_VERSION_V2,
    websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
    features=(FEATURE_HA_EXPORT_BINARY_V1,),
)
_MIXED_SELECTION = ProtocolSelection(
    version=PROTOCOL_VERSION_V2,
    websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
    features=(
        FEATURE_HA_EXPORT_BINARY_V1,
        FEATURE_HA_EXPORT_NUMERIC_V1,
    ),
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

    def __init__(
        self,
        response_builder=None,
        *,
        selection: ProtocolSelection = _SELECTION,
    ) -> None:
        self.sent: list[dict[str, object]] = []
        self._responses: deque[dict[str, object]] = deque()
        self._response_builder = response_builder
        self._never_respond = asyncio.Event()
        self.selection = selection

    def supports(self, feature: str) -> bool:
        """Return whether one optional application behavior was negotiated."""
        return self.selection.supports(feature)

    async def async_send(self, payload: dict[str, object]) -> None:
        """Record a payload and enqueue responses to apply requests."""
        self.sent.append(deepcopy(payload))
        if (
            payload.get("type") in {"apply", "binary_apply"}
            and self._response_builder is not None
        ):
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
    request = parse_apply(_SELECTION, payload)
    return [
        build_apply_result(
            _SELECTION,
            request.request_id,
            ApplyResultStatus.CONFIRMED,
            f"target-{request.action.capability.source.object_id}",
            request.action.capability.source,
        )
    ]


def _confirmed_binary_response(
    payload: dict[str, object],
) -> list[dict[str, object]]:
    """Confirm one binary apply request using its exact source."""
    request = parse_binary_apply(_BINARY_SELECTION, payload)
    return [
        build_binary_apply_result(
            _BINARY_SELECTION,
            request.request_id,
            ApplyResultStatus.CONFIRMED,
            f"target-{request.action.capability.source.object_id}",
            request.action.capability.source,
        )
    ]


def _confirmed_mixed_response(
    payload: dict[str, object],
) -> list[dict[str, object]]:
    """Confirm either independently negotiated application message."""
    if payload.get("type") == "binary_apply":
        return _confirmed_binary_response(payload)
    return _confirmed_response(payload)


def _configure_application(
    monkeypatch: pytest.MonkeyPatch,
    capabilities: list[Capability],
    storage: _MemoryStorage,
    exclusions: list[ExportExclusion] | None = None,
    *,
    included_kinds: frozenset[CapabilityKind] = frozenset({CapabilityKind.NUMERIC}),
    binary_storage: _MemoryStorage | None = None,
) -> None:
    """Inject deterministic source collection and catalog storage."""
    if exclusions is None:
        exclusions = []

    monkeypatch.setattr(
        app_module,
        "async_get_instance_id",
        AsyncMock(return_value="instance-1"),
    )

    def collect(_hass, *, instance_id: str, label_id: str, included_kinds):
        assert instance_id == "instance-1"
        assert label_id == "export-label"
        assert included_kinds == expected_kinds
        return ExportCollection(tuple(capabilities), tuple(exclusions))

    expected_kinds = included_kinds
    monkeypatch.setattr(app_module, "collect_export_selection", collect)

    def make_storage(_hass, *, entry_id: str, destination_id: str):
        assert entry_id == "entry-1"
        assert destination_id == "destination-1"
        return storage

    monkeypatch.setattr(app_module, "HomeAssistantCatalogStorage", make_storage)

    if binary_storage is not None:

        def make_binary_storage(_hass, *, entry_id: str, destination_id: str):
            assert entry_id == "entry-1"
            assert destination_id == "destination-1"
            return binary_storage

        monkeypatch.setattr(
            app_module,
            "HomeAssistantBinaryCatalogStorage",
            make_binary_storage,
        )


@pytest.mark.asyncio
async def test_application_is_inert_without_negotiated_export_feature() -> None:
    """Direct invocation cannot bypass authenticated application feature gates."""
    selection = ProtocolSelection(
        version=PROTOCOL_VERSION_V2,
        websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
        features=(),
    )
    session = _Session(selection=selection)

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert session.sent == []


@pytest.mark.asyncio
async def test_application_reconciles_binary_only_when_independently_negotiated(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A binary-only peer uses only the binary wire route and catalog."""
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    numeric_storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        [binary],
        numeric_storage,
        included_kinds=frozenset({CapabilityKind.BINARY}),
        binary_storage=binary_storage,
    )
    session = _Session(
        _confirmed_binary_response,
        selection=_BINARY_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    requests = [
        parse_binary_apply(_BINARY_SELECTION, payload)
        for payload in session.sent
        if payload.get("type") == "binary_apply"
    ]
    assert [request.action.capability for request in requests] == [binary]
    assert numeric_storage.document is None
    catalog = catalog_from_document(binary_storage.document)
    assert [record.capability for record in catalog.records] == [binary]
    assert ExportExclusionReason.CAPABILITY_KIND_NOT_ENABLED.value not in caplog.text


@pytest.mark.asyncio
async def test_application_reconciles_mixed_features_into_separate_catalogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Numeric and binary actions retain independent wire and storage state."""
    numeric = _capability("numeric-source")
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    numeric_storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        [numeric, binary],
        numeric_storage,
        included_kinds=frozenset({CapabilityKind.NUMERIC, CapabilityKind.BINARY}),
        binary_storage=binary_storage,
    )
    session = _Session(
        _confirmed_mixed_response,
        selection=_MIXED_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == [
        "apply",
        "binary_apply",
    ]
    assert [
        record.capability
        for record in catalog_from_document(numeric_storage.document).records
    ] == [numeric]
    assert [
        record.capability
        for record in catalog_from_document(binary_storage.document).records
    ] == [binary]


@pytest.mark.asyncio
async def test_binary_reconnect_uses_persisted_catalog_without_duplicate_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged binary reconnect is a no-op after its first commit."""
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    numeric_storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        [binary],
        numeric_storage,
        included_kinds=frozenset({CapabilityKind.BINARY}),
        binary_storage=binary_storage,
    )
    application = HomeAssistantExportApplication(_Hass())
    session = _Session(
        _confirmed_binary_response,
        selection=_BINARY_SELECTION,
    )

    await application.async_connected(session)
    await application.async_connected(session)

    assert [payload["type"] for payload in session.sent] == ["binary_apply"]
    assert len(binary_storage.saved_documents) == 1


@pytest.mark.asyncio
async def test_binary_unavailable_state_is_reasserted_on_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect restores runtime-only timeout through the binary route."""
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    capabilities = [binary]
    numeric_storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        numeric_storage,
        included_kinds=frozenset({CapabilityKind.BINARY}),
        binary_storage=binary_storage,
    )
    application = HomeAssistantExportApplication(_Hass())
    session = _Session(
        _confirmed_binary_response,
        selection=_BINARY_SELECTION,
    )
    await application.async_connected(session)

    capabilities[0] = replace(
        binary,
        value=None,
        availability=Availability.UNAVAILABLE,
    )
    await application.async_connected(session)
    await application.async_connected(session)

    requests = [
        parse_binary_apply(_BINARY_SELECTION, payload)
        for payload in session.sent
        if payload.get("type") == "binary_apply"
    ]
    assert [request.action.kind for request in requests] == [
        ReconciliationActionKind.CREATE,
        ReconciliationActionKind.MARK_UNAVAILABLE,
        ReconciliationActionKind.MARK_UNAVAILABLE,
    ]
    record = catalog_from_document(binary_storage.document).records[0]
    assert record.capability.availability is Availability.UNAVAILABLE
    assert record.capability.value is None


@pytest.mark.asyncio
async def test_application_filters_binary_and_commits_numeric(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only numeric capabilities are applied and binary exclusion is visible."""
    numeric = _capability("numeric-source")
    storage = _MemoryStorage()
    exclusion = ExportExclusion(
        "binary_sensor.binary_source",
        ExportExclusionReason.CAPABILITY_KIND_NOT_ENABLED,
    )
    _configure_application(monkeypatch, [numeric], storage, [exclusion])
    session = _Session(_confirmed_response)

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    requests = [
        parse_apply(_SELECTION, payload)
        for payload in session.sent
        if payload.get("type") == "apply"
    ]
    assert [request.action.capability for request in requests] == [numeric]
    catalog = catalog_from_document(storage.document)
    assert len(catalog.records) == 1
    assert catalog.records[0].capability == numeric
    assert len(storage.saved_documents) == 1
    assert "binary_sensor.binary_source" in caplog.text
    assert ExportExclusionReason.CAPABILITY_KIND_NOT_ENABLED.value in caplog.text


@pytest.mark.asyncio
async def test_exclusion_warnings_are_safe_deduplicated_and_can_recur(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unchanged reconnects stay quiet, while a resolved issue may recur."""
    exclusions = [
        ExportExclusion(
            "sensor.actionable_entity",
            ExportExclusionReason.INVALID_NUMERIC_STATE,
        )
    ]
    storage = _MemoryStorage()
    _configure_application(monkeypatch, [], storage, exclusions)
    application = HomeAssistantExportApplication(_Hass())
    session = _Session(_confirmed_response)

    await application.async_connected(session)
    await application.async_connected(session)

    warning = (
        "Domoticz export skipped directly labelled entity "
        "sensor.actionable_entity: sensor state is not a finite number"
    )
    assert caplog.text.count(warning) == 1
    assert "private-state-value" not in caplog.text

    exclusions.clear()
    await application.async_connected(session)
    exclusions.append(
        ExportExclusion(
            "sensor.actionable_entity",
            ExportExclusionReason.INVALID_NUMERIC_STATE,
        )
    )
    await application.async_connected(session)

    assert caplog.text.count(warning) == 2


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
        request = parse_apply(_SELECTION, payload)
        if request.action.capability.source == rejected.source:
            return [
                build_apply_result(
                    _SELECTION,
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
        parse_apply(_SELECTION, payload)
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
                _SELECTION,
                "request-different",
                ApplyResultStatus.CONFIRMED,
                "target-a",
                action.capability.source,
            )
        ]

    with pytest.raises(ProtocolError, match="invalid protocol message"):
        await DomoticzSessionTargetAdapter(_Session(responses)).async_apply(action)


@pytest.mark.asyncio
async def test_binary_adapter_uses_the_independent_binary_messages() -> None:
    """The binary adapter neither sends nor accepts numeric apply messages."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_capability("binary-source", kind=CapabilityKind.BINARY),
    )
    session = _Session(
        _confirmed_binary_response,
        selection=_BINARY_SELECTION,
    )

    confirmation = await DomoticzBinarySessionTargetAdapter(session).async_apply(action)

    assert session.sent[0]["type"] == "binary_apply"
    request = parse_binary_apply(_BINARY_SELECTION, session.sent[0])
    assert request.action == action
    assert confirmation.source == action.capability.source


@pytest.mark.asyncio
async def test_adapter_rejects_confirmation_for_different_source() -> None:
    """A confirmation must carry the action's exact source identity."""
    action = _create_action()

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        request = parse_apply(_SELECTION, payload)
        return [
            build_apply_result(
                _SELECTION,
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

    request_id = parse_apply(_SELECTION, session.sent[0]).request_id
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
        request = parse_apply(_SELECTION, payload)
        return [
            build_apply_result(
                _SELECTION,
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
