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
from custom_components.domoticz_sync.core import (
    reconciliation as reconciliation_module,  # noqa: E402
)
from custom_components.domoticz_sync.core.capabilities import (  # noqa: E402
    Availability,
    Capability,
    CapabilityKind,
    SourceIdentity,
)
from custom_components.domoticz_sync.core.catalog import (  # noqa: E402
    TargetCatalog,
    catalog_from_document,
    catalog_to_document,
)
from custom_components.domoticz_sync.core.execution import (  # noqa: E402
    TargetActionError,
)
from custom_components.domoticz_sync.core.protocol import (  # noqa: E402
    FEATURE_DOMOTICZ_INVENTORY_V1,
    FEATURE_HA_EXPORT_BINARY_V1,
    FEATURE_HA_EXPORT_NUMERIC_V1,
    PROTOCOL_VERSION_V2,
    WEBSOCKET_SUBPROTOCOL_V2,
    ApplyResultStatus,
    InventoryResult,
    InventoryResultStatus,
    InventoryTarget,
    InventoryUnit,
    ProtocolError,
    ProtocolSelection,
    build_apply_result,
    build_binary_apply_result,
    build_inventory_result,
    generate_nonce,
    parse_apply,
    parse_binary_apply,
    parse_inventory_request,
)
from custom_components.domoticz_sync.core.reconciliation import (  # noqa: E402
    ReconciliationAction,
    ReconciliationActionKind,
    TargetRecord,
    derive_domoticz_target_id,
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
_INVENTORY_SELECTION = ProtocolSelection(
    version=PROTOCOL_VERSION_V2,
    websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
    features=(
        FEATURE_DOMOTICZ_INVENTORY_V1,
        FEATURE_HA_EXPORT_NUMERIC_V1,
    ),
)
_INVENTORY_MIXED_SELECTION = ProtocolSelection(
    version=PROTOCOL_VERSION_V2,
    websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
    features=(
        FEATURE_DOMOTICZ_INVENTORY_V1,
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
        self.load_calls = 0

    async def async_load(self):
        """Return an isolated durable document."""
        self.load_calls += 1
        return deepcopy(self.document)

    async def async_save(self, document):
        """Replace the durable document."""
        self.document = deepcopy(document)
        self.saved_documents.append(deepcopy(document))


class _GatedSaveStorage(_MemoryStorage):
    """Pause persistence so concurrent application calls can be observed."""

    def __init__(self) -> None:
        super().__init__()
        self.save_entered = asyncio.Event()
        self.release_save = asyncio.Event()

    async def async_save(self, document):
        """Wait at the final create persistence boundary until released."""
        if len(self.saved_documents) == 1:
            self.save_entered.set()
            await self.release_save.wait()
        await super().async_save(document)


class _Session:
    """Sequential bridge application session with scripted responses."""

    entry_id = "entry-1"
    destination_id = "destination-1"

    def __init__(
        self,
        response_builder=None,
        *,
        selection: ProtocolSelection = _SELECTION,
        destination_id: str = "destination-1",
    ) -> None:
        self.sent: list[dict[str, object]] = []
        self._responses: deque[dict[str, object]] = deque()
        self._response_builder = response_builder
        self._never_respond = asyncio.Event()
        self.selection = selection
        self.destination_id = destination_id

    def supports(self, feature: str) -> bool:
        """Return whether one optional application behavior was negotiated."""
        return self.selection.supports(feature)

    async def async_send(self, payload: dict[str, object]) -> None:
        """Record a payload and enqueue responses to apply requests."""
        self.sent.append(deepcopy(payload))
        if (
            payload.get("type") in {"apply", "binary_apply", "inventory_request"}
            and self._response_builder is not None
        ):
            self._responses.extend(self._response_builder(payload))

    async def async_receive(self) -> dict[str, object]:
        """Return the next scripted payload or remain pending."""
        if self._responses:
            return self._responses.popleft()
        await self._never_respond.wait()
        raise AssertionError("unreachable")


class _StartObservedSession(_Session):
    """Signal when a session has reached its pre-lock feature gate."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started = asyncio.Event()

    def supports(self, feature: str) -> bool:
        """Record that the application call has started running."""
        self.started.set()
        return super().supports(feature)


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


def _inventory_unit(
    *,
    unit: int = 1,
    name: str = "sensor-a",
    type_id: int = 243,
    subtype: int = 31,
    switch_type: int = 0,
    used: bool = True,
    n_value: int = 0,
    s_value: str = "12.5",
    custom_option: str | None = "1;celsius",
    has_other_options: bool = False,
) -> InventoryUnit:
    """Build one representative strict Domoticz inventory unit."""
    return InventoryUnit(
        unit=unit,
        name=name,
        type=type_id,
        subtype=subtype,
        switch_type=switch_type,
        used=used,
        n_value=n_value,
        s_value=s_value,
        custom_option=custom_option,
        has_other_options=has_other_options,
    )


def _inventory_target(
    capability: Capability,
    *,
    units: tuple[InventoryUnit, ...] | None = None,
) -> InventoryTarget:
    """Build one deterministic inventory target for a source capability."""
    return InventoryTarget(
        target_id=derive_domoticz_target_id(capability.source),
        timed_out=False,
        units=units if units is not None else (_inventory_unit(name=capability.name),),
    )


def _inventory_page(
    selection: ProtocolSelection,
    request_id: str,
    *,
    page: int = 1,
    complete: bool = True,
    targets: tuple[InventoryTarget, ...] = (),
    status: InventoryResultStatus = InventoryResultStatus.CONFIRMED,
) -> dict[str, object]:
    """Build one exact scripted inventory result payload."""
    return build_inventory_result(
        selection,
        InventoryResult(
            request_id=request_id,
            status=status,
            page=page,
            complete=complete,
            targets=targets,
        ),
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


def _inventory_and_apply_responses(
    selection: ProtocolSelection,
    targets: tuple[InventoryTarget, ...] = (),
):
    """Build a responder for one inventory followed by normal apply traffic."""

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        if payload.get("type") == "inventory_request":
            request_id = parse_inventory_request(selection, payload)
            return [_inventory_page(selection, request_id, targets=targets)]
        if payload.get("type") == "binary_apply":
            request = parse_binary_apply(selection, payload)
            return [
                build_binary_apply_result(
                    selection,
                    request.request_id,
                    ApplyResultStatus.CONFIRMED,
                    derive_domoticz_target_id(request.action.capability.source),
                    request.action.capability.source,
                )
            ]
        request = parse_apply(selection, payload)
        return [
            build_apply_result(
                selection,
                request.request_id,
                ApplyResultStatus.CONFIRMED,
                derive_domoticz_target_id(request.action.capability.source),
                request.action.capability.source,
            )
        ]

    return responses


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

    if binary_storage is None:
        binary_storage = _MemoryStorage()

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
async def test_inventory_is_complete_before_either_catalog_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One staged snapshot precedes both independently persisted catalogs."""
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
        _inventory_and_apply_responses(_INVENTORY_MIXED_SELECTION),
        selection=_INVENTORY_MIXED_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == [
        "inventory_request",
        "apply",
        "binary_apply",
    ]
    assert numeric_storage.load_calls == 1
    assert binary_storage.load_calls == 1
    assert len(numeric_storage.saved_documents) == 2
    assert len(binary_storage.saved_documents) == 2


@pytest.mark.asyncio
async def test_inventory_accepts_interleaved_ping_before_terminal_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat traffic is answered while one overall inventory deadline runs."""
    storage = _MemoryStorage()
    _configure_application(monkeypatch, [], storage)
    ping_id = generate_nonce()

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        request_id = parse_inventory_request(_INVENTORY_SELECTION, payload)
        return [
            {"id": ping_id, "type": "ping"},
            _inventory_page(_INVENTORY_SELECTION, request_id),
        ]

    session = _Session(responses, selection=_INVENTORY_SELECTION)

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == [
        "inventory_request",
        "pong",
    ]
    assert session.sent[1] == {"id": ping_id, "type": "pong"}
    assert storage.load_calls == 1
    assert storage.saved_documents == []


@pytest.mark.parametrize(
    "failure",
    ("rejected", "request-mismatch", "page-gap", "duplicate", "malformed", "oversized"),
)
@pytest.mark.asyncio
async def test_invalid_inventory_never_loads_or_mutates_catalog(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Every invalid complete snapshot fails before storage or target access."""
    capability = _capability("sensor-a")
    target = _inventory_target(capability)
    storage = _MemoryStorage()
    _configure_application(monkeypatch, [capability], storage)

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        request_id = parse_inventory_request(_INVENTORY_SELECTION, payload)
        if failure == "rejected":
            return [
                _inventory_page(
                    _INVENTORY_SELECTION,
                    request_id,
                    status=InventoryResultStatus.REJECTED,
                )
            ]
        if failure == "request-mismatch":
            return [_inventory_page(_INVENTORY_SELECTION, "different-request")]
        if failure == "page-gap":
            return [
                _inventory_page(
                    _INVENTORY_SELECTION,
                    request_id,
                    page=2,
                    targets=(target,),
                )
            ]
        if failure == "duplicate":
            return [
                _inventory_page(
                    _INVENTORY_SELECTION,
                    request_id,
                    complete=False,
                    targets=(target,),
                ),
                _inventory_page(
                    _INVENTORY_SELECTION,
                    request_id,
                    page=2,
                    targets=(target,),
                ),
            ]
        valid = _inventory_page(_INVENTORY_SELECTION, request_id, targets=(target,))
        if failure == "malformed":
            valid["unexpected"] = True
        else:
            valid["targets"][0]["units"][0]["s_value"] = "x" * (61 * 1024)
        return [valid]

    session = _Session(responses, selection=_INVENTORY_SELECTION)

    with pytest.raises(ProtocolError):
        await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert storage.load_calls == 0
    assert storage.saved_documents == []
    assert [payload["type"] for payload in session.sent] == ["inventory_request"]


@pytest.mark.asyncio
async def test_incomplete_inventory_times_out_before_catalog_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-terminal prefix is never mistaken for an authoritative snapshot."""
    capability = _capability("sensor-a")
    storage = _MemoryStorage()
    _configure_application(monkeypatch, [capability], storage)
    monkeypatch.setattr(app_module, "INVENTORY_TIMEOUT", 0.01)

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        request_id = parse_inventory_request(_INVENTORY_SELECTION, payload)
        return [
            _inventory_page(
                _INVENTORY_SELECTION,
                request_id,
                complete=False,
                targets=(_inventory_target(capability),),
            )
        ]

    session = _Session(responses, selection=_INVENTORY_SELECTION)

    with pytest.raises(TimeoutError):
        await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert storage.load_calls == 0
    assert storage.saved_documents == []
    assert [payload["type"] for payload in session.sent] == ["inventory_request"]


@pytest.mark.asyncio
async def test_inventory_deadline_includes_blocked_request_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backpressure cannot postpone preflight or allow catalog access."""

    class _BlockedInventorySession(_Session):
        async def async_send(self, payload: dict[str, object]) -> None:
            self.sent.append(deepcopy(payload))
            await self._never_respond.wait()

    storage = _MemoryStorage()
    _configure_application(monkeypatch, [_capability("sensor-a")], storage)
    monkeypatch.setattr(app_module, "INVENTORY_TIMEOUT", 0.01)
    session = _BlockedInventorySession(selection=_INVENTORY_SELECTION)

    with pytest.raises(TimeoutError):
        await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert storage.load_calls == 0
    assert storage.saved_documents == []
    assert [payload["type"] for payload in session.sent] == ["inventory_request"]


@pytest.mark.asyncio
async def test_inventory_forces_reassert_without_rewriting_unchanged_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real matching target is re-read remotely without storage churn."""
    capability = _capability("sensor-a")
    target = _inventory_target(capability)
    storage = _MemoryStorage()
    storage.document = catalog_to_document(
        TargetCatalog([TargetRecord(target.target_id, capability)])
    )
    _configure_application(monkeypatch, [capability], storage)
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_SELECTION, (target,)),
        selection=_INVENTORY_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == [
        "inventory_request",
        "apply",
    ]
    request = parse_apply(_INVENTORY_SELECTION, session.sent[1])
    assert request.action.kind is ReconciliationActionKind.UPDATE
    assert request.action.target_id == target.target_id
    assert storage.load_calls == 1
    assert storage.saved_documents == []


@pytest.mark.asyncio
async def test_inventory_preflights_cross_kind_catalog_bindings_before_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cross-catalog target collision aborts after loads but before writes."""
    numeric = _capability("numeric-source")
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    shared_target = derive_domoticz_target_id(numeric.source)
    numeric_storage = _MemoryStorage()
    numeric_storage.document = catalog_to_document(
        TargetCatalog([TargetRecord(shared_target, numeric)])
    )
    binary_storage = _MemoryStorage()
    binary_storage.document = catalog_to_document(
        TargetCatalog([TargetRecord(shared_target, binary)])
    )
    _configure_application(
        monkeypatch,
        [numeric, binary],
        numeric_storage,
        included_kinds=frozenset({CapabilityKind.NUMERIC, CapabilityKind.BINARY}),
        binary_storage=binary_storage,
    )
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_MIXED_SELECTION),
        selection=_INVENTORY_MIXED_SELECTION,
    )

    with pytest.raises(ProtocolError, match="export reconciliation is unavailable"):
        await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert numeric_storage.load_calls == 1
    assert binary_storage.load_calls == 1
    assert numeric_storage.saved_documents == []
    assert binary_storage.saved_documents == []
    assert [payload["type"] for payload in session.sent] == ["inventory_request"]


@pytest.mark.asyncio
async def test_inventory_preflights_inactive_catalog_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inactive catalog still reserves remote-missing deterministic IDs."""
    target_id = "HA00000000000000000000000"
    current = _capability("current-source")
    stale = replace(
        _capability("stale-source", kind=CapabilityKind.BINARY),
        value=None,
        availability=Availability.UNAVAILABLE,
    )
    storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    binary_storage.document = catalog_to_document(
        TargetCatalog([TargetRecord(target_id, stale, stale=True)])
    )
    _configure_application(
        monkeypatch,
        [current],
        storage,
        binary_storage=binary_storage,
    )
    monkeypatch.setattr(
        reconciliation_module,
        "derive_domoticz_target_id",
        lambda _source: target_id,
    )
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_SELECTION),
        selection=_INVENTORY_SELECTION,
    )

    with pytest.raises(ProtocolError, match="export reconciliation is unavailable"):
        await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert storage.load_calls == 1
    assert binary_storage.load_calls == 1
    assert storage.saved_documents == []
    assert binary_storage.saved_documents == []
    assert [payload["type"] for payload in session.sent] == ["inventory_request"]


@pytest.mark.asyncio
async def test_same_destination_reconciliations_are_serialized_through_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement session waits before collection until persistence completes."""
    capability = _capability("sensor-a")
    storage = _GatedSaveStorage()
    _configure_application(monkeypatch, [capability], storage)
    original_collect = app_module.collect_export_selection
    collection_calls = 0

    def collect(hass, *, instance_id: str, label_id: str, included_kinds):
        nonlocal collection_calls
        collection_calls += 1
        return original_collect(
            hass,
            instance_id=instance_id,
            label_id=label_id,
            included_kinds=included_kinds,
        )

    monkeypatch.setattr(app_module, "collect_export_selection", collect)
    application = HomeAssistantExportApplication(_Hass())
    responses = _inventory_and_apply_responses(_INVENTORY_SELECTION)
    first = _Session(responses, selection=_INVENTORY_SELECTION)
    second = _StartObservedSession(responses, selection=_INVENTORY_SELECTION)

    first_task = asyncio.create_task(application.async_connected(first))
    await asyncio.wait_for(storage.save_entered.wait(), 1)
    second_task = asyncio.create_task(application.async_connected(second))
    await asyncio.wait_for(second.started.wait(), 1)
    try:
        assert collection_calls == 1
        assert second.sent == []
        assert storage.load_calls == 1
        assert len(storage.saved_documents) == 1
    finally:
        storage.release_save.set()
        await asyncio.gather(first_task, second_task)

    assert collection_calls == 2
    assert [payload["type"] for payload in first.sent] == [
        "inventory_request",
        "apply",
    ]
    assert [payload["type"] for payload in second.sent] == [
        "inventory_request",
        "apply",
    ]
    assert storage.load_calls == 2
    assert len(storage.saved_documents) == 2


@pytest.mark.asyncio
async def test_different_destinations_reconcile_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked save for one destination does not hold another destination."""
    capability = _capability("sensor-a")
    first_storage = _GatedSaveStorage()
    second_storage = _MemoryStorage()
    first_binary_storage = _MemoryStorage()
    second_binary_storage = _MemoryStorage()
    _configure_application(monkeypatch, [capability], first_storage)
    numeric_storages = {
        "destination-1": first_storage,
        "destination-2": second_storage,
    }
    binary_storages = {
        "destination-1": first_binary_storage,
        "destination-2": second_binary_storage,
    }

    def make_numeric_storage(_hass, *, entry_id: str, destination_id: str):
        assert entry_id == "entry-1"
        return numeric_storages[destination_id]

    def make_binary_storage(_hass, *, entry_id: str, destination_id: str):
        assert entry_id == "entry-1"
        return binary_storages[destination_id]

    monkeypatch.setattr(
        app_module,
        "HomeAssistantCatalogStorage",
        make_numeric_storage,
    )
    monkeypatch.setattr(
        app_module,
        "HomeAssistantBinaryCatalogStorage",
        make_binary_storage,
    )
    application = HomeAssistantExportApplication(_Hass())
    responses = _inventory_and_apply_responses(_INVENTORY_SELECTION)
    first = _Session(responses, selection=_INVENTORY_SELECTION)
    second = _Session(
        responses,
        selection=_INVENTORY_SELECTION,
        destination_id="destination-2",
    )

    first_task = asyncio.create_task(application.async_connected(first))
    await asyncio.wait_for(first_storage.save_entered.wait(), 1)
    second_task = asyncio.create_task(application.async_connected(second))
    try:
        await asyncio.wait_for(second_task, 1)
        assert not first_task.done()
        assert len(second_storage.saved_documents) == 2
        assert second_binary_storage.load_calls == 1
    finally:
        first_storage.release_save.set()
        await first_task

    assert len(first_storage.saved_documents) == 2
    assert first_binary_storage.load_calls == 1
    assert [payload["type"] for payload in second.sent] == [
        "inventory_request",
        "apply",
    ]


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
