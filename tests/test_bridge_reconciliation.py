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
    FEATURE_HA_EXPORT_CONTINUOUS_V1,
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
    SourceScope,
    TargetObservation,
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
_CONTINUOUS_INVENTORY_SELECTION = ProtocolSelection(
    version=PROTOCOL_VERSION_V2,
    websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
    features=(
        FEATURE_DOMOTICZ_INVENTORY_V1,
        FEATURE_HA_EXPORT_CONTINUOUS_V1,
        FEATURE_HA_EXPORT_NUMERIC_V1,
    ),
)
_CONTINUOUS_INVENTORY_MIXED_SELECTION = ProtocolSelection(
    version=PROTOCOL_VERSION_V2,
    websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
    features=(
        FEATURE_DOMOTICZ_INVENTORY_V1,
        FEATURE_HA_EXPORT_BINARY_V1,
        FEATURE_HA_EXPORT_CONTINUOUS_V1,
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


class _ControllableSaveStorage(_MemoryStorage):
    """Pause one selected save after earlier reconciliation has completed."""

    def __init__(self) -> None:
        super().__init__()
        self.gate_after_saves: int | None = None
        self.save_entered = asyncio.Event()
        self.release_save = asyncio.Event()

    async def async_save(self, document):
        """Wait at one configured save boundary, then resume normal storage."""
        if self.gate_after_saves == len(self.saved_documents):
            self.gate_after_saves = None
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


class _ControllableSession(_Session):
    """Wake a blocked receive when a test supplies a delayed response."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._response_available = asyncio.Event()

    async def async_send(self, payload: dict[str, object]) -> None:
        """Record a request and wake the receiver for scripted responses."""
        await super().async_send(payload)
        if self._responses:
            self._response_available.set()

    async def async_receive(self) -> dict[str, object]:
        """Wait until a response is supplied instead of waiting forever."""
        while not self._responses:
            await self._response_available.wait()
            self._response_available.clear()
        return self._responses.popleft()

    def enqueue(self, payload: dict[str, object]) -> None:
        """Supply one delayed protocol response."""
        self._responses.append(deepcopy(payload))
        self._response_available.set()


class _ExportChangeSubscription:
    """Observe one value-free export subscription and its cleanup."""

    def __init__(self) -> None:
        self.callback = None
        self.install_calls = 0
        self.unsubscribe_calls = 0
        self.active = False

    def install(self, _hass, *, label_id: str, on_change):
        """Capture the application callback using the configured label."""
        assert label_id == "export-label"
        self.install_calls += 1
        self.callback = on_change
        self.active = True

        def unsubscribe() -> None:
            if not self.active:
                return
            self.active = False
            self.unsubscribe_calls += 1

        return unsubscribe

    def fire(self) -> None:
        """Emit one value-free dirty hint while subscribed."""
        assert self.callback is not None
        if self.active:
            self.callback()


async def _async_wait_until(predicate, *, timeout_seconds: float = 1.0) -> None:
    """Yield until one deterministic asynchronous observation becomes true."""
    async with asyncio.timeout(timeout_seconds):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0)


async def _async_cancel_task(task: asyncio.Task[None]) -> None:
    """Cancel one long-lived application task without masking test failures."""
    if task.done():
        await asyncio.gather(task)
        return
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(task)


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


def _observations_with_unit_count(count: int) -> tuple[TargetObservation, ...]:
    """Build a compact valid observation set with one exact aggregate unit count."""
    observations = []
    remaining = count
    index = 0
    while remaining:
        unit_count = min(remaining, 255)
        observations.append(
            TargetObservation(
                f"remote-{index:03d}",
                tuple(range(1, unit_count + 1)),
            )
        )
        remaining -= unit_count
        index += 1
    return tuple(observations)


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


def _configure_continuous_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> _ExportChangeSubscription:
    """Install one observable subscription and remove the test debounce delay."""
    subscription = _ExportChangeSubscription()
    monkeypatch.setattr(
        app_module,
        "async_subscribe_export_changes",
        subscription.install,
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "CONTINUOUS_COALESCE_SECONDS",
        0,
        raising=False,
    )
    return subscription


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


@pytest.mark.parametrize(
    "features",
    (
        (
            FEATURE_HA_EXPORT_CONTINUOUS_V1,
            FEATURE_HA_EXPORT_NUMERIC_V1,
        ),
        (
            FEATURE_DOMOTICZ_INVENTORY_V1,
            FEATURE_HA_EXPORT_CONTINUOUS_V1,
        ),
    ),
    ids=("missing-inventory", "missing-export-kind"),
)
@pytest.mark.asyncio
async def test_continuous_requires_inventory_and_an_export_kind(
    features: tuple[str, ...],
) -> None:
    """Continuous mode fails closed without its complete safety prerequisites."""
    selection = ProtocolSelection(
        version=PROTOCOL_VERSION_V2,
        websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
        features=features,
    )
    session = _Session(selection=selection)

    with pytest.raises(ProtocolError, match="continuous export is unavailable"):
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
        await asyncio.gather(first_task)

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
async def test_continuous_updates_owned_numeric_and_binary_without_new_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One dirty snapshot sends only changed owned targets on the same session."""
    numeric = _capability("numeric-source")
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    capabilities = [numeric, binary]
    numeric_storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        numeric_storage,
        included_kinds=frozenset({CapabilityKind.NUMERIC, CapabilityKind.BINARY}),
        binary_storage=binary_storage,
    )
    subscription = _configure_continuous_subscription(monkeypatch)
    session = _Session(
        _inventory_and_apply_responses(_CONTINUOUS_INVENTORY_MIXED_SELECTION),
        selection=_CONTINUOUS_INVENTORY_MIXED_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("binary_apply")
                == 1
            )
        )
        assert not task.done()
        assert subscription.install_calls == 1

        capabilities[:] = [
            replace(numeric, name="Updated numeric", value=21.5),
            replace(binary, name="Updated binary", value=False),
        ]
        subscription.fire()

        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("binary_apply")
                == 2
            )
        )
        message_types = [payload["type"] for payload in session.sent]
        assert message_types == [
            "inventory_request",
            "apply",
            "binary_apply",
            "apply",
            "binary_apply",
        ]
        numeric_request = parse_apply(
            _CONTINUOUS_INVENTORY_MIXED_SELECTION,
            session.sent[-2],
        )
        binary_request = parse_binary_apply(
            _CONTINUOUS_INVENTORY_MIXED_SELECTION,
            session.sent[-1],
        )
        assert numeric_request.action.kind is ReconciliationActionKind.UPDATE
        assert numeric_request.action.capability == capabilities[0]
        assert binary_request.action.kind is ReconciliationActionKind.UPDATE
        assert binary_request.action.capability == capabilities[1]
        assert (
            catalog_from_document(numeric_storage.document).records[0].capability
            == capabilities[0]
        )
        assert (
            catalog_from_document(binary_storage.document).records[0].capability
            == capabilities[1]
        )
        assert numeric_storage.load_calls == 2
        assert binary_storage.load_calls == 2
    finally:
        await _async_cancel_task(task)

    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_relabel_stales_without_churn_and_reuses_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relabel-out never deletes, and relabel-in clears stale on the same target."""
    capability = _capability("sensor-a")
    capabilities = [capability]
    storage = _MemoryStorage()
    _configure_application(monkeypatch, capabilities, storage)
    subscription = _configure_continuous_subscription(monkeypatch)
    session = _Session(
        _inventory_and_apply_responses(_CONTINUOUS_INVENTORY_SELECTION),
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("apply") == 1
            )
        )
        target_id = catalog_from_document(storage.document).records[0].target_id

        capabilities.clear()
        subscription.fire()
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("apply") == 2
            )
        )
        removed = parse_apply(_CONTINUOUS_INVENTORY_SELECTION, session.sent[-1])
        assert removed.action.kind is ReconciliationActionKind.MARK_UNAVAILABLE
        assert removed.action.target_id == target_id
        assert removed.action.stale
        stale_record = catalog_from_document(storage.document).records[0]
        assert stale_record.target_id == target_id
        assert stale_record.stale

        subscription.fire()
        await _async_wait_until(lambda: storage.load_calls == 3)
        assert [payload.get("type") for payload in session.sent].count("apply") == 2

        returned = replace(capability, value=18.75)
        capabilities.append(returned)
        subscription.fire()
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("apply") == 3
            )
        )
        restored = parse_apply(_CONTINUOUS_INVENTORY_SELECTION, session.sent[-1])
        assert restored.action.kind is ReconciliationActionKind.UPDATE
        assert restored.action.target_id == target_id
        record = catalog_from_document(storage.document).records[0]
        assert record.target_id == target_id
        assert record.capability == returned
        assert not record.stale
        assert [payload.get("type") for payload in session.sent].count(
            "inventory_request"
        ) == 1
    finally:
        await _async_cancel_task(task)

    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_uncataloged_source_reconnects_before_any_batch_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh inventory is required before any newly desired source can write."""
    owned = _capability("owned")
    capabilities = [owned]
    storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        storage,
        binary_storage=binary_storage,
    )
    subscription = _configure_continuous_subscription(monkeypatch)
    session = _Session(
        _inventory_and_apply_responses(_CONTINUOUS_INVENTORY_SELECTION),
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    await _async_wait_until(
        lambda: [payload.get("type") for payload in session.sent].count("apply") == 1
    )
    baseline_document = deepcopy(storage.document)
    baseline_saves = len(storage.saved_documents)
    baseline_sent = deepcopy(session.sent)
    capabilities[:] = [replace(owned, value=99.0), _capability("new-source")]
    subscription.fire()

    with pytest.raises(ConnectionError, match="fresh inventory is required"):
        await asyncio.gather(task)

    assert session.sent == baseline_sent
    assert storage.document == baseline_document
    assert len(storage.saved_documents) == baseline_saves
    assert binary_storage.saved_documents == []
    assert storage.load_calls == 2
    assert binary_storage.load_calls == 2
    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_baseline_collision_waits_for_leave_and_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An initial remote-only collision stays blocked without a reconnect loop."""
    blocked = _capability("blocked-source")
    capabilities = [blocked]
    storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        storage,
        binary_storage=binary_storage,
    )
    subscription = _configure_continuous_subscription(monkeypatch)
    session = _Session(
        _inventory_and_apply_responses(
            _CONTINUOUS_INVENTORY_SELECTION,
            (_inventory_target(blocked),),
        ),
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    await _async_wait_until(lambda: storage.load_calls == 1)
    subscription.fire()
    await _async_wait_until(lambda: storage.load_calls == 2)
    assert not task.done()
    assert [payload["type"] for payload in session.sent] == ["inventory_request"]
    assert storage.saved_documents == []

    capabilities.clear()
    subscription.fire()
    await _async_wait_until(lambda: storage.load_calls == 3)
    assert not task.done()

    capabilities.append(blocked)
    subscription.fire()
    with pytest.raises(ConnectionError, match="fresh inventory is required"):
        await asyncio.gather(task)

    assert [payload["type"] for payload in session.sent] == ["inventory_request"]
    assert storage.saved_documents == []
    assert binary_storage.saved_documents == []
    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_dirty_during_apply_sends_first_then_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-flight value completes before one coalesced latest-state follow-up."""
    initial = _capability("sensor-a")
    capabilities = [initial]
    storage = _MemoryStorage()
    _configure_application(monkeypatch, capabilities, storage)
    subscription = _configure_continuous_subscription(monkeypatch)
    live_requests = []

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        if payload.get("type") == "inventory_request":
            request_id = parse_inventory_request(
                _CONTINUOUS_INVENTORY_SELECTION,
                payload,
            )
            return [
                _inventory_page(
                    _CONTINUOUS_INVENTORY_SELECTION,
                    request_id,
                )
            ]
        request = parse_apply(_CONTINUOUS_INVENTORY_SELECTION, payload)
        if request.action.kind is ReconciliationActionKind.CREATE:
            return [
                build_apply_result(
                    _CONTINUOUS_INVENTORY_SELECTION,
                    request.request_id,
                    ApplyResultStatus.CONFIRMED,
                    derive_domoticz_target_id(request.action.capability.source),
                    request.action.capability.source,
                )
            ]
        live_requests.append(request)
        if len(live_requests) == 1:
            return []
        return [
            build_apply_result(
                _CONTINUOUS_INVENTORY_SELECTION,
                request.request_id,
                ApplyResultStatus.CONFIRMED,
                request.action.target_id,
                request.action.capability.source,
            )
        ]

    session = _ControllableSession(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("apply") == 1
            )
        )
        capabilities[0] = replace(initial, value=20.0)
        subscription.fire()
        await _async_wait_until(lambda: len(live_requests) == 1)

        capabilities[0] = replace(initial, value=30.0)
        subscription.fire()
        capabilities[0] = replace(initial, value=40.0)
        subscription.fire()

        first = live_requests[0]
        session.enqueue(
            build_apply_result(
                _CONTINUOUS_INVENTORY_SELECTION,
                first.request_id,
                ApplyResultStatus.CONFIRMED,
                first.action.target_id,
                first.action.capability.source,
            )
        )
        await _async_wait_until(
            lambda: (
                catalog_from_document(storage.document).records[0].capability.value
                == 40.0
            )
        )

        assert [request.action.capability.value for request in live_requests] == [
            20.0,
            40.0,
        ]
        assert [payload.get("type") for payload in session.sent].count(
            "inventory_request"
        ) == 1
    finally:
        await _async_cancel_task(task)

    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_coalescing_deadline_is_fixed_from_first_dirty_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One production-length window coalesces a burst into the latest snapshot."""
    initial = _capability("sensor-a")
    capabilities = [initial]
    storage = _MemoryStorage()
    _configure_application(monkeypatch, capabilities, storage)
    subscription = _ExportChangeSubscription()
    monkeypatch.setattr(
        app_module,
        "async_subscribe_export_changes",
        subscription.install,
    )
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    requested_delays: list[float] = []

    async def controlled_sleep(delay: float) -> None:
        requested_delays.append(delay)
        sleep_started.set()
        await release_sleep.wait()

    monkeypatch.setattr(
        app_module,
        "asyncio",
        SimpleNamespace(
            Event=asyncio.Event,
            Lock=asyncio.Lock,
            gather=asyncio.gather,
            sleep=controlled_sleep,
            timeout=asyncio.timeout,
        ),
    )
    session = _Session(
        _inventory_and_apply_responses(_CONTINUOUS_INVENTORY_SELECTION),
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("apply") == 1
            )
        )
        capabilities[0] = replace(initial, value=20.0)
        subscription.fire()
        await asyncio.wait_for(sleep_started.wait(), 1)

        assert requested_delays == [0.25]
        assert [payload.get("type") for payload in session.sent].count("apply") == 1

        capabilities[0] = replace(initial, value=30.0)
        subscription.fire()
        capabilities[0] = replace(initial, value=40.0)
        subscription.fire()
        await asyncio.sleep(0)

        assert requested_delays == [0.25]
        assert [payload.get("type") for payload in session.sent].count("apply") == 1

        release_sleep.set()
        await _async_wait_until(
            lambda: (
                catalog_from_document(storage.document).records[0].capability.value
                == 40.0
            )
        )
        requests = [
            parse_apply(_CONTINUOUS_INVENTORY_SELECTION, payload)
            for payload in session.sent
            if payload.get("type") == "apply"
        ]
        assert [request.action.capability.value for request in requests] == [
            12.5,
            40.0,
        ]
        assert requested_delays == [0.25]
        assert storage.load_calls == 2
    finally:
        release_sleep.set()
        await _async_cancel_task(task)

    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_dirty_during_save_runs_one_fresh_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new value arriving at persistence waits, then reconciles from fresh state."""
    initial = _capability("sensor-a")
    capabilities = [initial]
    storage = _ControllableSaveStorage()
    _configure_application(monkeypatch, capabilities, storage)
    subscription = _configure_continuous_subscription(monkeypatch)
    session = _Session(
        _inventory_and_apply_responses(_CONTINUOUS_INVENTORY_SELECTION),
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("apply") == 1
            )
        )
        await _async_wait_until(lambda: len(storage.saved_documents) == 2)
        storage.gate_after_saves = 2

        capabilities[0] = replace(initial, value=20.0)
        subscription.fire()
        await asyncio.wait_for(storage.save_entered.wait(), 1)

        capabilities[0] = replace(initial, value=30.0)
        subscription.fire()
        storage.release_save.set()
        await _async_wait_until(
            lambda: (
                catalog_from_document(storage.document)
                .get(initial.source)
                .capability.value
                == 30.0
            )
        )

        requests = [
            parse_apply(_CONTINUOUS_INVENTORY_SELECTION, payload)
            for payload in session.sent
            if payload.get("type") == "apply"
        ]
        assert [request.action.capability.value for request in requests] == [
            12.5,
            20.0,
            30.0,
        ]
    finally:
        storage.release_save.set()
        await _async_cancel_task(task)


@pytest.mark.asyncio
async def test_continuous_subscription_precedes_initial_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dirty event at the collection barrier cannot lose the latest state."""
    initial = _capability("sensor-a")
    latest = replace(initial, value=42.0)
    capabilities = [initial]
    storage = _MemoryStorage()
    _configure_application(monkeypatch, capabilities, storage)
    subscription = _configure_continuous_subscription(monkeypatch)
    original_collect = app_module.collect_export_selection
    collection_calls = 0

    def collect(hass, *, instance_id: str, label_id: str, included_kinds):
        nonlocal collection_calls
        collection_calls += 1
        if collection_calls == 1:
            capabilities[0] = latest
            subscription.fire()
        return original_collect(
            hass,
            instance_id=instance_id,
            label_id=label_id,
            included_kinds=included_kinds,
        )

    monkeypatch.setattr(app_module, "collect_export_selection", collect)
    session = _Session(
        _inventory_and_apply_responses(_CONTINUOUS_INVENTORY_SELECTION),
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(lambda: storage.load_calls >= 2)
        requests = [
            parse_apply(_CONTINUOUS_INVENTORY_SELECTION, payload)
            for payload in session.sent
            if payload.get("type") == "apply"
        ]
        assert len(requests) == 1
        assert requests[0].action.capability == latest
        assert catalog_from_document(storage.document).records[0].capability == latest
        assert storage.load_calls == 2
        assert not task.done()
    finally:
        await _async_cancel_task(task)

    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_unchanged_rejection_waits_for_desired_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrelated dirty cycles do not retry one unchanged rejected target state."""
    rejected = _capability("rejected-source")
    unrelated = _capability("unrelated-source")
    capabilities = [rejected, unrelated]
    storage = _MemoryStorage()
    _configure_application(monkeypatch, capabilities, storage)
    subscription = _configure_continuous_subscription(monkeypatch)
    rejected_values: list[float] = []

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        if payload.get("type") == "inventory_request":
            request_id = parse_inventory_request(
                _CONTINUOUS_INVENTORY_SELECTION,
                payload,
            )
            return [
                _inventory_page(
                    _CONTINUOUS_INVENTORY_SELECTION,
                    request_id,
                )
            ]
        request = parse_apply(_CONTINUOUS_INVENTORY_SELECTION, payload)
        if (
            request.action.kind is not ReconciliationActionKind.CREATE
            and request.action.capability.source == rejected.source
        ):
            assert isinstance(request.action.capability.value, float)
            rejected_values.append(request.action.capability.value)
            return [
                build_apply_result(
                    _CONTINUOUS_INVENTORY_SELECTION,
                    request.request_id,
                    ApplyResultStatus.REJECTED,
                    None,
                    None,
                )
            ]
        return [
            build_apply_result(
                _CONTINUOUS_INVENTORY_SELECTION,
                request.request_id,
                ApplyResultStatus.CONFIRMED,
                derive_domoticz_target_id(request.action.capability.source),
                request.action.capability.source,
            )
        ]

    session = _Session(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(
            lambda: (
                [payload.get("type") for payload in session.sent].count("apply") == 2
            )
        )
        capabilities[:] = [
            replace(rejected, value=20.0),
            replace(unrelated, value=20.0),
        ]
        subscription.fire()
        await _async_wait_until(lambda: rejected_values == [20.0])
        await _async_wait_until(
            lambda: (
                catalog_from_document(storage.document)
                .get(unrelated.source)
                .capability.value
                == 20.0
            )
        )

        capabilities[1] = replace(unrelated, value=30.0)
        subscription.fire()
        await _async_wait_until(
            lambda: (
                catalog_from_document(storage.document)
                .get(unrelated.source)
                .capability.value
                == 30.0
            )
        )
        assert rejected_values == [20.0]

        capabilities[0] = replace(rejected, value=40.0)
        subscription.fire()
        await _async_wait_until(lambda: rejected_values == [20.0, 40.0])

        capabilities[1] = replace(unrelated, value=40.0)
        subscription.fire()
        await _async_wait_until(
            lambda: (
                catalog_from_document(storage.document)
                .get(unrelated.source)
                .capability.value
                == 40.0
            )
        )
        assert rejected_values == [20.0, 40.0]
    finally:
        await _async_cancel_task(task)


@pytest.mark.asyncio
async def test_continuous_create_rejection_is_suppressed_until_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending CREATE becomes the same desired UPDATE, but a new session retries."""
    rejected = _capability("rejected-source")
    unrelated = _capability("unrelated-source")
    capabilities = [rejected, unrelated]
    storage = _MemoryStorage()
    _configure_application(monkeypatch, capabilities, storage)
    subscription = _configure_continuous_subscription(monkeypatch)
    rejected_kinds: list[ReconciliationActionKind] = []

    def responses(payload: dict[str, object]) -> list[dict[str, object]]:
        if payload.get("type") == "inventory_request":
            request_id = parse_inventory_request(
                _CONTINUOUS_INVENTORY_SELECTION,
                payload,
            )
            return [
                _inventory_page(
                    _CONTINUOUS_INVENTORY_SELECTION,
                    request_id,
                )
            ]
        request = parse_apply(_CONTINUOUS_INVENTORY_SELECTION, payload)
        if request.action.capability.source == rejected.source:
            rejected_kinds.append(request.action.kind)
            return [
                build_apply_result(
                    _CONTINUOUS_INVENTORY_SELECTION,
                    request.request_id,
                    ApplyResultStatus.REJECTED,
                    None,
                    None,
                )
            ]
        return [
            build_apply_result(
                _CONTINUOUS_INVENTORY_SELECTION,
                request.request_id,
                ApplyResultStatus.CONFIRMED,
                derive_domoticz_target_id(request.action.capability.source),
                request.action.capability.source,
            )
        ]

    application = HomeAssistantExportApplication(_Hass())
    first_session = _Session(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    first_task = asyncio.create_task(application.async_connected(first_session))

    try:
        await _async_wait_until(lambda: len(rejected_kinds) == 1)
        assert rejected_kinds == [ReconciliationActionKind.CREATE]
        assert catalog_from_document(storage.document).get(rejected.source).pending

        capabilities[1] = replace(unrelated, value=20.0)
        subscription.fire()
        await _async_wait_until(
            lambda: (
                catalog_from_document(storage.document)
                .get(unrelated.source)
                .capability.value
                == 20.0
            )
        )
        assert rejected_kinds == [ReconciliationActionKind.CREATE]
    finally:
        await _async_cancel_task(first_task)

    second_session = _Session(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    second_task = asyncio.create_task(application.async_connected(second_session))
    try:
        await _async_wait_until(lambda: len(rejected_kinds) == 2)
        assert rejected_kinds == [
            ReconciliationActionKind.CREATE,
            ReconciliationActionKind.UPDATE,
        ]
    finally:
        await _async_cancel_task(second_task)

    assert subscription.install_calls == 2
    assert subscription.unsubscribe_calls == 2


@pytest.mark.asyncio
async def test_inventory_capacity_admits_one_global_slot_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact lifetime limit admits the same source across both kind orders."""
    numeric = _capability("numeric-source")
    binary = _capability("binary-source", kind=CapabilityKind.BINARY)
    capabilities = [numeric, binary]
    numeric_storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        numeric_storage,
        included_kinds=frozenset({CapabilityKind.NUMERIC, CapabilityKind.BINARY}),
        binary_storage=binary_storage,
    )
    monkeypatch.setattr(app_module, "MAX_INVENTORY_TARGETS", 2)
    unrelated = InventoryTarget(
        target_id="remote-unrelated",
        timed_out=False,
        units=(_inventory_unit(name="Remote unrelated"),),
    )
    session = _Session(
        _inventory_and_apply_responses(
            _INVENTORY_MIXED_SELECTION,
            (unrelated,),
        ),
        selection=_INVENTORY_MIXED_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == [
        "inventory_request",
        "binary_apply",
    ]
    request = parse_binary_apply(_INVENTORY_MIXED_SELECTION, session.sent[-1])
    assert request.action.capability.source == binary.source
    assert numeric_storage.saved_documents == []
    assert len(binary_storage.saved_documents) == 2


@pytest.mark.asyncio
async def test_inventory_capacity_blocks_one_over_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full remote lifetime budget cannot partially save or apply a new source."""
    capability = _capability("blocked-source")
    storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        [capability],
        storage,
        binary_storage=binary_storage,
    )
    monkeypatch.setattr(app_module, "MAX_INVENTORY_TARGETS", 1)
    unrelated = InventoryTarget(
        target_id="remote-unrelated",
        timed_out=False,
        units=(_inventory_unit(name="Remote unrelated"),),
    )
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_SELECTION, (unrelated,)),
        selection=_INVENTORY_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == ["inventory_request"]
    assert storage.saved_documents == []
    assert binary_storage.saved_documents == []


@pytest.mark.asyncio
async def test_inventory_capacity_counts_the_inactive_kind_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable binary identity reserves capacity during numeric-only export."""
    binary = _capability("reserved-binary", kind=CapabilityKind.BINARY)
    numeric = _capability("blocked-numeric")
    numeric_storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    binary_storage.document = catalog_to_document(
        TargetCatalog([TargetRecord(derive_domoticz_target_id(binary.source), binary)])
    )
    _configure_application(
        monkeypatch,
        [numeric],
        numeric_storage,
        binary_storage=binary_storage,
    )
    monkeypatch.setattr(app_module, "MAX_INVENTORY_TARGETS", 1)
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_SELECTION),
        selection=_INVENTORY_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == ["inventory_request"]
    assert numeric_storage.saved_documents == []
    assert binary_storage.saved_documents == []
    assert numeric_storage.load_calls == 1
    assert binary_storage.load_calls == 1


@pytest.mark.asyncio
async def test_inventory_capacity_allows_owned_recovery_at_exact_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing catalog-owned target may recover without growing its identity set."""
    previous = _capability("owned-source")
    current = replace(previous, value=25.0)
    storage = _MemoryStorage()
    storage.document = catalog_to_document(
        TargetCatalog(
            [TargetRecord(derive_domoticz_target_id(previous.source), previous)]
        )
    )
    _configure_application(monkeypatch, [current], storage)
    monkeypatch.setattr(app_module, "MAX_INVENTORY_TARGETS", 1)
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_SELECTION),
        selection=_INVENTORY_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    assert [payload["type"] for payload in session.sent] == [
        "inventory_request",
        "apply",
    ]
    request = parse_apply(_INVENTORY_SELECTION, session.sent[-1])
    assert request.action.kind is ReconciliationActionKind.UPDATE
    assert request.action.capability == current
    assert len(storage.saved_documents) == 1
    assert (
        catalog_from_document(storage.document).get(previous.source).capability
        == current
    )


def test_inventory_unit_capacity_blocks_a_create_at_1024_units() -> None:
    """A new target cannot make the next complete inventory unrepresentable."""
    capability = _capability("new-source")
    catalogs = {
        CapabilityKind.NUMERIC: TargetCatalog(),
        CapabilityKind.BINARY: TargetCatalog(),
    }

    admission = app_module._admit_inventory_creates(
        SourceScope("home_assistant", "instance-1"),
        (capability,),
        _observations_with_unit_count(1024),
        catalogs,
        frozenset({CapabilityKind.NUMERIC}),
    )

    assert admission.capabilities == ()
    assert admission.blocked_sources == frozenset({capability.source})
    assert admission.blocked_durable_sources == frozenset()


def test_inventory_unit_capacity_admits_one_cross_kind_create_at_1023() -> None:
    """The final unit slot is assigned globally by stable source identity."""
    numeric = _capability("z-numeric")
    binary = _capability("a-binary", kind=CapabilityKind.BINARY)
    catalogs = {
        CapabilityKind.NUMERIC: TargetCatalog(),
        CapabilityKind.BINARY: TargetCatalog(),
    }

    admission = app_module._admit_inventory_creates(
        SourceScope("home_assistant", "instance-1"),
        (numeric, binary),
        _observations_with_unit_count(1023),
        catalogs,
        frozenset({CapabilityKind.NUMERIC, CapabilityKind.BINARY}),
    )

    assert admission.capabilities == (binary,)
    assert admission.blocked_sources == frozenset({numeric.source})
    assert admission.blocked_durable_sources == frozenset()


def test_inventory_unit_capacity_prioritizes_selected_durable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One recovery slot is reserved for selected ownership before dormant state."""
    selected = _capability("z-selected")
    dormant = _capability("a-dormant", kind=CapabilityKind.BINARY)
    catalogs = {
        CapabilityKind.NUMERIC: TargetCatalog(
            [TargetRecord(derive_domoticz_target_id(selected.source), selected)]
        ),
        CapabilityKind.BINARY: TargetCatalog(
            [TargetRecord(derive_domoticz_target_id(dormant.source), dormant)]
        ),
    }
    monkeypatch.setattr(app_module, "MAX_INVENTORY_UNITS", 1)

    admission = app_module._admit_inventory_creates(
        SourceScope("home_assistant", "instance-1"),
        (selected,),
        (),
        catalogs,
        frozenset({CapabilityKind.NUMERIC}),
    )

    assert admission.capabilities == (selected,)
    assert admission.blocked_sources == frozenset({dormant.source})
    assert admission.blocked_durable_sources == frozenset({dormant.source})


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_parent", (False, True))
async def test_full_unit_capacity_blocks_only_owned_recovery(
    monkeypatch: pytest.MonkeyPatch,
    empty_parent: bool,
) -> None:
    """A missing or empty owned target waits while an unrelated update commits."""
    owned = _capability("owned-source")
    unrelated = _capability("unrelated-source")
    changed_owned = replace(owned, value=20.0)
    changed_unrelated = replace(unrelated, value=30.0)
    storage = _MemoryStorage()
    storage.document = catalog_to_document(
        TargetCatalog(
            [
                TargetRecord(derive_domoticz_target_id(owned.source), owned),
                TargetRecord(
                    derive_domoticz_target_id(unrelated.source),
                    unrelated,
                ),
            ]
        )
    )
    _configure_application(
        monkeypatch,
        [changed_owned, changed_unrelated],
        storage,
    )
    monkeypatch.setattr(app_module, "MAX_INVENTORY_UNITS", 1)
    targets = [_inventory_target(unrelated)]
    if empty_parent:
        targets.append(_inventory_target(owned, units=()))
    targets.sort(key=lambda target: target.target_id)
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_SELECTION, tuple(targets)),
        selection=_INVENTORY_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    requests = [
        parse_apply(_INVENTORY_SELECTION, payload)
        for payload in session.sent
        if payload.get("type") == "apply"
    ]
    assert [request.action.capability.source for request in requests] == [
        unrelated.source
    ]
    catalog = catalog_from_document(storage.document)
    assert catalog.get(owned.source).capability == owned
    assert catalog.get(unrelated.source).capability == changed_unrelated


@pytest.mark.asyncio
async def test_one_free_unit_slot_admits_owned_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One reserved unit slot admits a catalog-owned missing target atomically."""
    previous = _capability("owned-source")
    current = replace(previous, value=20.0)
    storage = _MemoryStorage()
    storage.document = catalog_to_document(
        TargetCatalog(
            [TargetRecord(derive_domoticz_target_id(previous.source), previous)]
        )
    )
    _configure_application(monkeypatch, [current], storage)
    monkeypatch.setattr(app_module, "MAX_INVENTORY_UNITS", 2)
    unrelated = InventoryTarget(
        target_id="remote-unrelated",
        timed_out=False,
        units=(_inventory_unit(name="Remote unrelated"),),
    )
    session = _Session(
        _inventory_and_apply_responses(_INVENTORY_SELECTION, (unrelated,)),
        selection=_INVENTORY_SELECTION,
    )

    await HomeAssistantExportApplication(_Hass()).async_connected(session)

    requests = [
        parse_apply(_INVENTORY_SELECTION, payload)
        for payload in session.sent
        if payload.get("type") == "apply"
    ]
    assert len(requests) == 1
    assert requests[0].action.kind is ReconciliationActionKind.UPDATE
    assert requests[0].action.capability == current
    assert (
        catalog_from_document(storage.document).get(previous.source).capability
        == current
    )


@pytest.mark.asyncio
async def test_continuous_capacity_baseline_waits_for_leave_and_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A capacity-blocked source does not reconnect-loop while still selected."""
    blocked = _capability("blocked-source")
    capabilities = [blocked]
    storage = _MemoryStorage()
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        storage,
        binary_storage=binary_storage,
    )
    subscription = _configure_continuous_subscription(monkeypatch)
    monkeypatch.setattr(app_module, "MAX_INVENTORY_TARGETS", 1)
    unrelated = InventoryTarget(
        target_id="remote-unrelated",
        timed_out=False,
        units=(_inventory_unit(name="Remote unrelated"),),
    )
    session = _Session(
        _inventory_and_apply_responses(
            _CONTINUOUS_INVENTORY_SELECTION,
            (unrelated,),
        ),
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    task = asyncio.create_task(
        HomeAssistantExportApplication(_Hass()).async_connected(session)
    )

    try:
        await _async_wait_until(lambda: storage.load_calls == 1)
        subscription.fire()
        await _async_wait_until(lambda: storage.load_calls == 2)
        assert not task.done()
        assert [payload["type"] for payload in session.sent] == ["inventory_request"]

        capabilities.clear()
        subscription.fire()
        await _async_wait_until(lambda: storage.load_calls == 3)
        assert not task.done()

        capabilities.append(blocked)
        subscription.fire()
        with pytest.raises(ConnectionError, match="fresh inventory is required"):
            await asyncio.gather(task)
    finally:
        if not task.done():
            await _async_cancel_task(task)

    assert [payload["type"] for payload in session.sent] == ["inventory_request"]
    assert storage.saved_documents == []
    assert binary_storage.saved_documents == []
    assert subscription.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_continuous_blocked_owned_recovery_waits_for_reentry_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active blocked recovery stays quiet, then reconnects after re-entry."""
    owned = _capability("owned-source")
    capabilities = [owned]
    storage = _MemoryStorage()
    storage.document = catalog_to_document(
        TargetCatalog([TargetRecord(derive_domoticz_target_id(owned.source), owned)])
    )
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        storage,
        binary_storage=binary_storage,
    )
    subscription = _configure_continuous_subscription(monkeypatch)
    monkeypatch.setattr(app_module, "MAX_INVENTORY_UNITS", 1)
    unrelated = InventoryTarget(
        target_id="remote-unrelated",
        timed_out=False,
        units=(_inventory_unit(name="Remote unrelated"),),
    )
    responses = _inventory_and_apply_responses(
        _CONTINUOUS_INVENTORY_SELECTION,
        (unrelated,),
    )
    application = HomeAssistantExportApplication(_Hass())
    first_session = _Session(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    first_task = asyncio.create_task(application.async_connected(first_session))

    try:
        await _async_wait_until(lambda: storage.load_calls == 1)
        subscription.fire()
        await _async_wait_until(lambda: storage.load_calls == 2)
        assert not first_task.done()
        assert [payload["type"] for payload in first_session.sent] == [
            "inventory_request"
        ]

        capabilities.clear()
        subscription.fire()
        await _async_wait_until(lambda: storage.load_calls == 3)
        assert not first_task.done()
        assert [payload["type"] for payload in first_session.sent] == [
            "inventory_request"
        ]

        capabilities.append(owned)
        subscription.fire()
        with pytest.raises(ConnectionError, match="fresh inventory is required"):
            await asyncio.gather(first_task)
    finally:
        if not first_task.done():
            await _async_cancel_task(first_task)

    baseline_loads = storage.load_calls
    second_session = _Session(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    second_task = asyncio.create_task(application.async_connected(second_session))
    try:
        await _async_wait_until(lambda: storage.load_calls == baseline_loads + 1)
        assert not second_task.done()
        assert [payload["type"] for payload in second_session.sent] == [
            "inventory_request"
        ]
    finally:
        await _async_cancel_task(second_task)

    assert storage.saved_documents == []
    assert binary_storage.saved_documents == []
    assert subscription.install_calls == 2
    assert subscription.unsubscribe_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_parent", (False, True))
async def test_continuous_blocked_dormant_recovery_requires_fresh_inventory(
    monkeypatch: pytest.MonkeyPatch,
    empty_parent: bool,
) -> None:
    """First entry of an unreserved durable source reconnects before any write."""
    owned = _capability("owned-source")
    capabilities: list[Capability] = []
    storage = _MemoryStorage()
    storage.document = catalog_to_document(
        TargetCatalog([TargetRecord(derive_domoticz_target_id(owned.source), owned)])
    )
    binary_storage = _MemoryStorage()
    _configure_application(
        monkeypatch,
        capabilities,
        storage,
        binary_storage=binary_storage,
    )
    subscription = _configure_continuous_subscription(monkeypatch)
    monkeypatch.setattr(app_module, "MAX_INVENTORY_UNITS", 1)
    targets = [
        InventoryTarget(
            target_id="remote-unrelated",
            timed_out=False,
            units=(_inventory_unit(name="Remote unrelated"),),
        )
    ]
    if empty_parent:
        targets.append(_inventory_target(owned, units=()))
    targets.sort(key=lambda target: target.target_id)
    responses = _inventory_and_apply_responses(
        _CONTINUOUS_INVENTORY_SELECTION,
        tuple(targets),
    )
    application = HomeAssistantExportApplication(_Hass())
    first_session = _Session(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    first_task = asyncio.create_task(application.async_connected(first_session))

    await _async_wait_until(lambda: storage.load_calls == 1)
    capabilities.append(owned)
    subscription.fire()
    with pytest.raises(ConnectionError, match="fresh inventory is required"):
        await asyncio.gather(first_task)

    assert [payload["type"] for payload in first_session.sent] == ["inventory_request"]
    assert storage.saved_documents == []
    assert binary_storage.saved_documents == []

    baseline_loads = storage.load_calls
    second_session = _Session(
        responses,
        selection=_CONTINUOUS_INVENTORY_SELECTION,
    )
    second_task = asyncio.create_task(application.async_connected(second_session))
    try:
        await _async_wait_until(lambda: storage.load_calls == baseline_loads + 1)
        assert not second_task.done()
        assert [payload["type"] for payload in second_session.sent] == [
            "inventory_request"
        ]
    finally:
        await _async_cancel_task(second_task)

    assert subscription.install_calls == 2
    assert subscription.unsubscribe_calls == 2


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
