"""End-to-end coverage for the Domoticz plugin and Home Assistant bridge."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from aiohttp import WSCloseCode, WSMsgType  # noqa: E402
from aiohttp import client as aiohttp_client  # noqa: E402
from homeassistant.components.binary_sensor import (  # noqa: E402
    BinarySensorDeviceClass,
)
from homeassistant.components.sensor import (  # noqa: E402
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (  # noqa: E402
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    UnitOfEnergy,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402
from homeassistant.helpers.instance_id import (  # noqa: E402
    async_get as async_get_instance_id,
)
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)
from pytest_homeassistant_custom_component.typing import (  # noqa: E402
    ClientSessionGenerator,
)

from custom_components.domoticz_sync import bridge as bridge_module  # noqa: E402
from custom_components.domoticz_sync import (  # noqa: E402
    bridge_reconciliation as bridge_reconciliation_module,
)
from custom_components.domoticz_sync.bridge import (  # noqa: E402
    BridgeApplicationSession,
    DomoticzBridgeManager,
    DomoticzBridgeView,
)
from custom_components.domoticz_sync.bridge_reconciliation import (  # noqa: E402
    HomeAssistantExportApplication,
)
from custom_components.domoticz_sync.catalog_storage import (  # noqa: E402
    HomeAssistantBinaryCatalogStorage,
    HomeAssistantCatalogStorage,
)
from custom_components.domoticz_sync.const import (  # noqa: E402
    CONF_EXPORT_LABEL_ID,
    DOMAIN,
    EXPORT_LABEL_NAME,
)
from custom_components.domoticz_sync.core.capabilities import (  # noqa: E402
    CapabilityKind,
)
from custom_components.domoticz_sync.core.catalog import (  # noqa: E402
    catalog_from_document,
)
from custom_components.domoticz_sync.core.protocol import (  # noqa: E402
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
    FEATURE_DOMOTICZ_INVENTORY_V1,
    FEATURE_HA_EXPORT_CONTINUOUS_V1,
    ApplyResultStatus,
    InventoryResult,
    InventoryTarget,
    ProtocolSelection,
    assemble_inventory_results,
    canonical_json_loads,
    generate_link_id,
    generate_pairing_key,
    parse_apply,
    parse_apply_result,
    parse_binary_apply,
    parse_binary_apply_result,
    parse_inventory_request,
    parse_inventory_result,
    verify_envelope,
)
from custom_components.domoticz_sync.core.reconciliation import (  # noqa: E402
    derive_domoticz_target_id,
)
from custom_components.domoticz_sync.home_assistant_source import (  # noqa: E402
    collect_export_selection,
)

ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


@dataclass(frozen=True)
class _InventoryExchange:
    """One authenticated, fully assembled inventory observed by the test."""

    request_id: str
    pages: tuple[InventoryResult, ...]
    targets: tuple[InventoryTarget, ...]


def _assert_inventory_negotiation_enabled(
    plugin_module: ModuleType,
) -> None:
    """Require both runtime peers to advertise the active inventory feature."""
    assert FEATURE_DOMOTICZ_INVENTORY_V1 in (bridge_module.SUPPORTED_V2_FEATURES)
    assert FEATURE_DOMOTICZ_INVENTORY_V1 in (
        plugin_module.wire_protocol.SUPPORTED_V2_FEATURES
    )


class _FakeConnection:
    """Translate Domoticz connection callbacks into an in-memory send queue."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.sent: list[dict[str, object]] = []
        self.connecting = False
        self.connected = False
        self.disconnected = False

    def Connect(self) -> None:
        self.connecting = True

    def Connected(self) -> bool:
        return self.connected

    def Connecting(self) -> bool:
        return self.connecting

    def Send(self, document: dict[str, object]) -> None:
        self.sent.append(document)

    def Disconnect(self) -> None:
        self.connecting = False
        self.connected = False
        self.disconnected = True


class _FakeUnit:
    """In-memory DomoticzEx unit with observable persistence calls."""

    def __init__(self, domoticz: _FakeDomoticz, **kwargs: object) -> None:
        self._domoticz = domoticz
        self.Name = kwargs.get("Name")
        self.Unit = kwargs.get("Unit", 1)
        self.Type = kwargs.get("Type", 0)
        self.SubType = kwargs.get("Subtype", kwargs.get("SubType", 0))
        self.SwitchType = kwargs.get("Switchtype", kwargs.get("SwitchType", 0))
        self.Options = dict(kwargs.get("Options", {}))
        self.Used = kwargs.get("Used", 0)
        self.nValue = kwargs.get("nValue", 0)
        self.sValue = kwargs.get("sValue", "")
        self.updates: list[dict[str, object]] = []
        self.refreshes = 0

    def Update(self, **kwargs: object) -> None:
        self.updates.append(dict(kwargs))

    def Refresh(self) -> None:
        self.refreshes += 1


class _FakeDevice:
    """Container matching DomoticzEx's extended device model."""

    def __init__(self, target_id: str) -> None:
        self.DeviceID = target_id
        self.TimedOut = 0
        self.Units: dict[int, _FakeUnit] = {}


class _FakeUnitCreator:
    """Deferred DomoticzEx Unit creator."""

    def __init__(
        self,
        domoticz: _FakeDomoticz,
        kwargs: dict[str, object],
    ) -> None:
        self._domoticz = domoticz
        self._kwargs = kwargs

    def Create(self) -> _FakeUnit:
        self._domoticz.create_calls.append(dict(self._kwargs))
        target_id = self._kwargs.get("DeviceID")
        assert isinstance(target_id, str)
        unit = _FakeUnit(self._domoticz, **self._kwargs)
        assert isinstance(unit.Unit, int)
        device = self._domoticz.devices.setdefault(
            target_id,
            _FakeDevice(target_id),
        )
        device.Units[unit.Unit] = unit
        return unit


class _FakeDomoticz(ModuleType):
    """Minimal DomoticzEx module used to load the real root plugin."""

    def __init__(self) -> None:
        super().__init__("DomoticzEx")
        self.configuration: dict[str, object] = {}
        self.connections: list[_FakeConnection] = []
        self.devices: dict[str, _FakeDevice] = {}
        self.create_calls: list[dict[str, object]] = []
        self.logs: list[str] = []
        self.statuses: list[str] = []
        self.errors: list[str] = []

    def Configuration(
        self,
        config: object = _MISSING,
    ) -> dict[str, object]:
        if config is not _MISSING:
            assert isinstance(config, dict)
            self.configuration = dict(config)
        return dict(self.configuration)

    def Connection(self, **kwargs: object) -> _FakeConnection:
        connection = _FakeConnection(**kwargs)
        self.connections.append(connection)
        return connection

    def Heartbeat(self, _seconds: int) -> None:
        pass

    def Unit(self, **kwargs: object) -> _FakeUnitCreator:
        return _FakeUnitCreator(self, kwargs)

    def Log(self, message: str) -> None:
        self.logs.append(message)

    def Status(self, message: str) -> None:
        self.statuses.append(message)

    def Error(self, message: str) -> None:
        self.errors.append(message)


def _load_plugin(
    monkeypatch: pytest.MonkeyPatch,
    *,
    address: str,
    port: int,
    link_id: str,
    pairing_key: str,
) -> tuple[ModuleType, _FakeDomoticz]:
    """Load root plugin.py with the test's callback-native Domoticz module."""
    fake_domoticz = _FakeDomoticz()
    monkeypatch.setitem(sys.modules, "DomoticzEx", fake_domoticz)

    module_name = "domoticz_plugin_bridge_integration"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    specification = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "plugin.py",
    )
    assert specification is not None
    assert specification.loader is not None
    plugin_module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, module_name, plugin_module)
    specification.loader.exec_module(plugin_module)
    plugin_module.Parameters = {
        "Address": address,
        "Port": str(port),
        "Mode1": "WS",
        "Mode2": link_id,
        "Mode3": pairing_key,
        "HomeFolder": str(ROOT),
    }
    plugin_module.Devices = fake_domoticz.devices
    return plugin_module, fake_domoticz


def _next_text_payload(
    connection: _FakeConnection,
    position: int,
) -> tuple[str, int]:
    """Take the next Domoticz WebSocket text send from the fake connection."""
    document = connection.sent[position]
    payload = document.get("Payload")
    assert isinstance(payload, str)
    return payload, position + 1


async def _receive_text(websocket: object) -> str:
    """Receive one real aiohttp WebSocket text frame with a bounded wait."""
    async with asyncio.timeout(2):
        message = await websocket.receive()
    assert message.type is WSMsgType.TEXT
    assert isinstance(message.data, str)
    return message.data


def _selected_protocol(plugin: object) -> ProtocolSelection:
    """Translate the plugin's isolated vendored selection into the HA core."""
    selected = plugin._protocol_selection
    return ProtocolSelection(
        version=selected.version,
        websocket_subprotocol=selected.websocket_subprotocol,
        features=tuple(selected.features),
    )


def _assert_continuous_negotiation_enabled(
    plugin_module: ModuleType,
) -> None:
    """Require both runtime peers to advertise active continuous export."""
    assert FEATURE_HA_EXPORT_CONTINUOUS_V1 in bridge_module.SUPPORTED_V2_FEATURES
    assert (
        FEATURE_HA_EXPORT_CONTINUOUS_V1
        in plugin_module.wire_protocol.SUPPORTED_V2_FEATURES
    )


async def _receive_application_text(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
) -> tuple[str, int]:
    """Receive application traffic while transparently servicing heartbeats."""
    while True:
        payload_text = await _receive_text(websocket)
        selection = _selected_protocol(plugin)
        verified = verify_envelope(
            plugin._session_key,
            canonical_json_loads(payload_text),
            protocol_version=selection.version,
            expected_direction=DIRECTION_HA_TO_DOMOTICZ,
            expected_session_id=plugin._session_id,
            last_sequence=plugin._in_sequence,
        )
        payload = verified.payload
        if not isinstance(payload, dict) or payload.get("type") not in {
            "ping",
            "pong",
        }:
            return payload_text, send_position

        sends_before = len(connection.sent)
        plugin.onMessage(connection, {"Payload": payload_text})
        if len(connection.sent) > sends_before:
            heartbeat_reply, send_position = _next_text_payload(
                connection,
                send_position,
            )
            await websocket.send_str(heartbeat_reply)


async def _assert_no_application_payload(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
    *,
    duration: float = 0.1,
) -> int:
    """Service heartbeats while proving no application request is emitted."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return send_position
        try:
            async with asyncio.timeout(remaining):
                payload_text = await websocket.receive_str()
        except TimeoutError:
            return send_position

        selection = _selected_protocol(plugin)
        verified = verify_envelope(
            plugin._session_key,
            canonical_json_loads(payload_text),
            protocol_version=selection.version,
            expected_direction=DIRECTION_HA_TO_DOMOTICZ,
            expected_session_id=plugin._session_id,
            last_sequence=plugin._in_sequence,
        )
        payload = verified.payload
        assert isinstance(payload, dict)
        assert payload.get("type") in {"ping", "pong"}, "unexpected application request"
        sends_before = len(connection.sent)
        plugin.onMessage(connection, {"Payload": payload_text})
        if len(connection.sent) > sends_before:
            heartbeat_reply, send_position = _next_text_payload(
                connection,
                send_position,
            )
            await websocket.send_str(heartbeat_reply)


async def _wait_for_controlled_close(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
) -> int:
    """Service only heartbeats until the bridge requests a clean reconnect."""
    async with asyncio.timeout(2):
        while True:
            message = await websocket.receive()
            if message.type is WSMsgType.CLOSE:
                assert message.data == WSCloseCode.GOING_AWAY
                return send_position
            assert message.type is WSMsgType.TEXT
            assert isinstance(message.data, str)

            selection = _selected_protocol(plugin)
            verified = verify_envelope(
                plugin._session_key,
                canonical_json_loads(message.data),
                protocol_version=selection.version,
                expected_direction=DIRECTION_HA_TO_DOMOTICZ,
                expected_session_id=plugin._session_id,
                last_sequence=plugin._in_sequence,
            )
            payload = verified.payload
            assert isinstance(payload, dict)
            assert payload.get("type") in {"ping", "pong"}, (
                "application write preceded controlled reconnect"
            )
            sends_before = len(connection.sent)
            plugin.onMessage(connection, {"Payload": message.data})
            if len(connection.sent) > sends_before:
                heartbeat_reply, send_position = _next_text_payload(
                    connection,
                    send_position,
                )
                await websocket.send_str(heartbeat_reply)


async def _exchange_complete_inventory(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
) -> tuple[int, _InventoryExchange]:
    """Exchange and independently verify one complete signed inventory."""
    selection = _selected_protocol(plugin)
    assert selection.supports(FEATURE_DOMOTICZ_INVENTORY_V1)

    request_text, send_position = await _receive_application_text(
        plugin,
        connection,
        websocket,
        send_position,
    )
    request_document = canonical_json_loads(request_text)
    request = verify_envelope(
        plugin._session_key,
        request_document,
        protocol_version=selection.version,
        expected_direction=DIRECTION_HA_TO_DOMOTICZ,
        expected_session_id=plugin._session_id,
        last_sequence=plugin._in_sequence,
    )
    request_id = parse_inventory_request(selection, request.payload)
    response_sequence = plugin._out_sequence
    plugin.onMessage(connection, {"Payload": request_text})

    pages = []
    while True:
        result_text, send_position = _next_text_payload(
            connection,
            send_position,
        )
        result_document = canonical_json_loads(result_text)
        verified = verify_envelope(
            plugin._session_key,
            result_document,
            protocol_version=selection.version,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=plugin._session_id,
            last_sequence=response_sequence,
        )
        response_sequence = verified.sequence
        result = parse_inventory_result(selection, verified.payload)
        assert result.request_id == request_id
        pages.append(result)
        await websocket.send_str(result_text)
        if result.complete:
            break

        # A partial snapshot cannot authorize an apply. A multi-page test below
        # makes this assertion observable with a selected HA source present.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await websocket.receive()

    targets = assemble_inventory_results(
        selection,
        request_id,
        tuple(pages),
    )
    assert pages[-1].complete is True
    return send_position, _InventoryExchange(
        request_id=request_id,
        pages=tuple(pages),
        targets=targets,
    )


class _ObservedApplication:
    """Record successful runs while delegating to the real application."""

    def __init__(self, application: HomeAssistantExportApplication) -> None:
        self._application = application
        self._condition = asyncio.Condition()
        self._next_call_id = 0
        self._session_call_ids: dict[int, int] = {}
        self._settled_call_ids: set[int] = set()
        self.attempted_calls = 0
        self.completed_calls = 0
        self.failure_type_chains: list[tuple[str, ...]] = []
        self.failure_frames: list[tuple[tuple[str, str, int], ...]] = []
        continuous_runner = application._async_run_continuous

        async def observed_continuous_runner(
            session: BridgeApplicationSession,
            **kwargs: object,
        ) -> None:
            await self._async_record_completion(session)
            await continuous_runner(session, **kwargs)

        application._async_run_continuous = observed_continuous_runner

    async def _async_record_completion(
        self,
        session: BridgeApplicationSession,
    ) -> None:
        """Record completion of connect-time work before a persistent loop."""
        call_id = self._session_call_ids[id(session)]
        async with self._condition:
            if call_id in self._settled_call_ids:
                return
            self._settled_call_ids.add(call_id)
            self.attempted_calls += 1
            self.completed_calls += 1
            self._condition.notify_all()

    async def async_connected(
        self,
        session: BridgeApplicationSession,
    ) -> None:
        self._next_call_id += 1
        call_id = self._next_call_id
        session_key = id(session)
        self._session_call_ids[session_key] = call_id
        try:
            await self._application.async_connected(session)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            type_chain = []
            current: BaseException | None = error
            while current is not None:
                type_chain.append(type(current).__name__)
                current = current.__cause__
            frames = tuple(
                (Path(frame.filename).name, frame.name, frame.lineno)
                for frame in traceback.extract_tb(error.__traceback__)
            )
            async with self._condition:
                if call_id not in self._settled_call_ids:
                    self._settled_call_ids.add(call_id)
                    self.attempted_calls += 1
                self.failure_type_chains.append(tuple(type_chain))
                self.failure_frames.append(frames)
                self._condition.notify_all()
            raise
        else:
            await self._async_record_completion(session)
        finally:
            if self._session_call_ids.get(session_key) == call_id:
                self._session_call_ids.pop(session_key)

    async def async_wait_for_calls(self, expected: int) -> None:
        """Wait until the real application has completed enough sessions."""
        async with asyncio.timeout(2):
            async with self._condition:
                await self._condition.wait_for(lambda: self.attempted_calls >= expected)
        assert self.completed_calls >= expected, (
            "bridge application failed with exception types "
            f"{self.failure_type_chains[-1]} at {self.failure_frames[-1]}"
        )


class _NoopApplication:
    """Keep the authenticated transport alive without application traffic."""

    async def async_connected(self, _session: BridgeApplicationSession) -> None:
        """Return after accepting the negotiated application session."""


class _ObservedBridgeView(DomoticzBridgeView):
    """Expose completion of each real HTTP WebSocket request handler."""

    def __init__(self, manager: DomoticzBridgeManager) -> None:
        super().__init__(manager)
        self._condition = asyncio.Condition()
        self.completed_requests = 0

    async def get(self, request: object) -> object:
        try:
            return await super().get(request)
        finally:
            async with self._condition:
                self.completed_requests += 1
                self._condition.notify_all()

    async def async_wait_for_requests(self, expected: int) -> None:
        """Wait until the complete view handler has returned."""
        async with asyncio.timeout(2):
            async with self._condition:
                await self._condition.wait_for(
                    lambda: self.completed_requests >= expected
                )


async def _open_plugin_connection(
    plugin_module: ModuleType,
    fake_domoticz: _FakeDomoticz,
    client: object,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    object,
    _FakeConnection,
    object,
    int,
    _InventoryExchange | None,
]:
    """Connect the real plugin through authentication and application ready."""
    plugin = plugin_module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = fake_domoticz.connections[-1]
    connection.connecting = False
    connection.connected = True
    plugin.onConnect(connection, 0, "connected")

    upgrade_request = connection.sent[0]
    request_headers = upgrade_request["Headers"]
    assert isinstance(request_headers, dict)
    upgrade_key = request_headers["Sec-WebSocket-Key"]
    assert isinstance(upgrade_key, str)
    protocol_header = request_headers["Sec-WebSocket-Protocol"]
    assert isinstance(protocol_header, str)
    protocols = tuple(
        protocol.strip() for protocol in protocol_header.split(",") if protocol.strip()
    )
    decoded_upgrade_key = base64.b64decode(upgrade_key, validate=True)

    with monkeypatch.context() as websocket_key_patch:
        websocket_key_patch.setattr(
            aiohttp_client,
            "os",
            SimpleNamespace(urandom=lambda size: decoded_upgrade_key),
        )
        websocket = await client.ws_connect(
            upgrade_request["URL"],
            origin=request_headers["Origin"],
            protocols=protocols,
            compress=0,
        )

    response = websocket._response
    plugin.onMessage(
        connection,
        {
            "Status": str(response.status),
            "Headers": dict(response.headers),
        },
    )

    send_position = 1
    hello, send_position = _next_text_payload(connection, send_position)
    await websocket.send_str(hello)
    plugin.onMessage(
        connection,
        {"Payload": await _receive_text(websocket)},
    )

    authenticate, send_position = _next_text_payload(
        connection,
        send_position,
    )
    await websocket.send_str(authenticate)
    plugin.onMessage(
        connection,
        {"Payload": await _receive_text(websocket)},
    )

    application_ready, send_position = _next_text_payload(
        connection,
        send_position,
    )
    await websocket.send_str(application_ready)
    plugin.onMessage(
        connection,
        {"Payload": await _receive_text(websocket)},
    )

    assert plugin.phase == plugin_module.PHASE_READY
    inventory = None
    if _selected_protocol(plugin).supports(FEATURE_DOMOTICZ_INVENTORY_V1):
        send_position, inventory = await _exchange_complete_inventory(
            plugin,
            connection,
            websocket,
            send_position,
        )
    return plugin, connection, websocket, send_position, inventory


async def _exchange_one_apply(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
    application: _ObservedApplication,
    expected_application_call: int,
) -> int:
    """Pass one signed apply and its signed result across the real socket."""
    try:
        payload, send_position = await _receive_application_text(
            plugin,
            connection,
            websocket,
            send_position,
        )
    except AssertionError:
        await application.async_wait_for_calls(expected_application_call)
        raise
    plugin.onMessage(connection, {"Payload": payload})
    result, send_position = _next_text_payload(connection, send_position)
    await websocket.send_str(result)
    return send_position


async def _exchange_observed_numeric_apply(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
) -> tuple[int, object, object]:
    """Exchange and independently parse one signed numeric apply and result."""
    selection = _selected_protocol(plugin)
    request_text, send_position = await _receive_application_text(
        plugin,
        connection,
        websocket,
        send_position,
    )
    request = parse_apply(
        selection,
        verify_envelope(
            plugin._session_key,
            canonical_json_loads(request_text),
            protocol_version=selection.version,
            expected_direction=DIRECTION_HA_TO_DOMOTICZ,
            expected_session_id=plugin._session_id,
            last_sequence=plugin._in_sequence,
        ).payload,
    )
    response_sequence = plugin._out_sequence
    plugin.onMessage(connection, {"Payload": request_text})

    result_text, send_position = _next_text_payload(connection, send_position)
    result = parse_apply_result(
        selection,
        verify_envelope(
            plugin._session_key,
            canonical_json_loads(result_text),
            protocol_version=selection.version,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=plugin._session_id,
            last_sequence=response_sequence,
        ).payload,
    )
    await websocket.send_str(result_text)
    return send_position, request, result


async def _exchange_observed_binary_apply(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
) -> tuple[int, object, object]:
    """Exchange and independently parse one signed binary apply and result."""
    selection = _selected_protocol(plugin)
    request_text, send_position = await _receive_application_text(
        plugin,
        connection,
        websocket,
        send_position,
    )
    request = parse_binary_apply(
        selection,
        verify_envelope(
            plugin._session_key,
            canonical_json_loads(request_text),
            protocol_version=selection.version,
            expected_direction=DIRECTION_HA_TO_DOMOTICZ,
            expected_session_id=plugin._session_id,
            last_sequence=plugin._in_sequence,
        ).payload,
    )
    response_sequence = plugin._out_sequence
    plugin.onMessage(connection, {"Payload": request_text})

    result_text, send_position = _next_text_payload(connection, send_position)
    result = parse_binary_apply_result(
        selection,
        verify_envelope(
            plugin._session_key,
            canonical_json_loads(result_text),
            protocol_version=selection.version,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=plugin._session_id,
            last_sequence=response_sequence,
        ).payload,
    )
    await websocket.send_str(result_text)
    return send_position, request, result


async def _close_plugin_connection(
    plugin: object,
    websocket: object,
    manager: DomoticzBridgeManager,
    view: _ObservedBridgeView,
    expected_request: int,
) -> None:
    """Close both ends and wait for bridge session cleanup."""
    await websocket.close()
    plugin.onStop()
    for _ in range(20):
        if await manager.async_active_session_count() == 0:
            break
        await asyncio.sleep(0)
    assert await manager.async_active_session_count() == 0
    await view.async_wait_for_requests(expected_request)


@pytest.mark.asyncio
async def test_root_plugin_and_real_bridge_reach_ready_and_exchange_ping(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped plugin interoperates with the registered HA endpoint."""
    manager = DomoticzBridgeManager(_NoopApplication())
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="integration-entry",
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    hass.http.register_view(DomoticzBridgeView(manager))
    client = await hass_client_no_auth()
    endpoint = client.make_url("/")
    assert endpoint.port is not None

    plugin_module, fake_domoticz = _load_plugin(
        monkeypatch,
        address=endpoint.host,
        port=endpoint.port,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    plugin = plugin_module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = fake_domoticz.connections[-1]
    connection.connecting = False
    connection.connected = True
    plugin.onConnect(connection, 0, "connected")

    upgrade_request = connection.sent[0]
    assert upgrade_request["URL"] == plugin_module._ENDPOINT
    request_headers = upgrade_request["Headers"]
    assert isinstance(request_headers, dict)
    upgrade_key = request_headers["Sec-WebSocket-Key"]
    assert isinstance(upgrade_key, str)
    protocol_header = request_headers["Sec-WebSocket-Protocol"]
    assert isinstance(protocol_header, str)
    protocols = tuple(
        protocol.strip() for protocol in protocol_header.split(",") if protocol.strip()
    )
    decoded_upgrade_key = base64.b64decode(upgrade_key, validate=True)

    # aiohttp normally generates its own key. Supplying the plugin's key here
    # lets the real server response flow back through the real plugin validator.
    with monkeypatch.context() as websocket_key_patch:
        websocket_key_patch.setattr(
            aiohttp_client,
            "os",
            SimpleNamespace(urandom=lambda size: decoded_upgrade_key),
        )
        websocket = await client.ws_connect(
            upgrade_request["URL"],
            origin=request_headers["Origin"],
            protocols=protocols,
            compress=0,
        )

    try:
        response = websocket._response
        plugin.onMessage(
            connection,
            {
                "Status": str(response.status),
                "Headers": dict(response.headers),
            },
        )

        send_position = 1
        hello, send_position = _next_text_payload(connection, send_position)
        await websocket.send_str(hello)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        authenticate, send_position = _next_text_payload(
            connection,
            send_position,
        )
        await websocket.send_str(authenticate)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        application_ready, send_position = _next_text_payload(
            connection,
            send_position,
        )
        await websocket.send_str(application_ready)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        for _ in range(20):
            if await manager.async_is_ready(link_id):
                break
            await asyncio.sleep(0)
        assert plugin.phase == plugin_module.PHASE_READY
        assert await manager.async_is_ready(link_id)

        for _ in range(plugin_module._PING_INTERVAL_TICKS):
            plugin.onHeartbeat()
        ping, send_position = _next_text_payload(connection, send_position)
        await websocket.send_str(ping)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        assert plugin.phase == plugin_module.PHASE_READY
        assert plugin._pending_ping_id is None
        assert send_position == len(connection.sent)
        assert fake_domoticz.errors == []
    finally:
        await websocket.close()
        plugin.onStop()

    for _ in range(20):
        if await manager.async_active_session_count() == 0:
            break
        await asyncio.sleep(0)
    assert await manager.async_active_session_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "source_unique_id",
        "source_name",
        "device_class",
        "state_class",
        "unit_of_measurement",
        "expected_type",
        "expected_subtype",
        "expected_options",
    ),
    [
        (
            "garden_temperature",
            "Garden temperature",
            SensorDeviceClass.TEMPERATURE,
            SensorStateClass.MEASUREMENT,
            UnitOfTemperature.CELSIUS,
            80,
            5,
            {},
        ),
        (
            "daily_energy",
            "Daily energy",
            SensorDeviceClass.ENERGY,
            SensorStateClass.TOTAL_INCREASING,
            UnitOfEnergy.KILO_WATT_HOUR,
            243,
            31,
            {"Custom": "1;kwh"},
        ),
    ],
    ids=("native-temperature", "custom-energy"),
)
async def test_real_bridge_reconciles_numeric_sensor_across_reconnects(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    source_unique_id: str,
    source_name: str,
    device_class: SensorDeviceClass,
    state_class: SensorStateClass,
    unit_of_measurement: str,
    expected_type: int,
    expected_subtype: int,
    expected_options: dict[str, str],
) -> None:
    """Reconcile native and fallback numerics without duplicate targets."""
    export_label = lr.async_get(hass).async_create(EXPORT_LABEL_NAME)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="integration-entry",
        data={CONF_EXPORT_LABEL_ID: export_label.label_id},
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    source_entry = registry.async_get_or_create(
        "sensor",
        "integration_test",
        source_unique_id,
        suggested_object_id=source_unique_id,
        capabilities={ATTR_STATE_CLASS: state_class},
        original_device_class=device_class,
        original_name=source_name,
        unit_of_measurement=unit_of_measurement,
    )
    registry.async_update_entity(
        source_entry.entity_id,
        labels={export_label.label_id},
    )
    state_attributes = {
        ATTR_DEVICE_CLASS: device_class,
        ATTR_FRIENDLY_NAME: source_name,
        ATTR_STATE_CLASS: state_class,
        ATTR_UNIT_OF_MEASUREMENT: unit_of_measurement,
    }
    hass.states.async_set(
        source_entry.entity_id,
        "12.5",
        state_attributes,
    )

    application = _ObservedApplication(HomeAssistantExportApplication(hass))
    manager = DomoticzBridgeManager(application)
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id=entry.entry_id,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    bridge_view = _ObservedBridgeView(manager)
    hass.http.register_view(bridge_view)
    client = await hass_client_no_auth()
    endpoint = client.make_url("/")
    assert endpoint.port is not None
    plugin_module, fake_domoticz = _load_plugin(
        monkeypatch,
        address=endpoint.host,
        port=endpoint.port,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    _assert_inventory_negotiation_enabled(plugin_module)

    # Force a two-page snapshot so the test observes that no apply is sent
    # after page 1. Empty containers are valid hardware-scoped inventory and
    # remain unrelated to the HA catalog.
    for index in range(65):
        target_id = f"FOREIGN{index:018d}"
        fake_domoticz.devices[target_id] = _FakeDevice(target_id)
    foreign_devices = dict(fake_domoticz.devices)

    (
        first,
        first_connection,
        first_websocket,
        first_position,
        first_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    destination_id = first._destination_id
    assert isinstance(destination_id, str)
    assert first_inventory is not None
    assert len(first_inventory.pages) == 2
    assert len(first_inventory.targets) == 65
    assert all(not target.units for target in first_inventory.targets)
    try:
        first_position = await _exchange_one_apply(
            first,
            first_connection,
            first_websocket,
            first_position,
            application,
            1,
        )
        await application.async_wait_for_calls(1)

        storage = HomeAssistantCatalogStorage(
            hass,
            entry_id=entry.entry_id,
            destination_id=destination_id,
        )
        catalog = catalog_from_document(await storage.async_load())
        assert len(catalog.records) == 1
        assert catalog.records[0].capability.state_class == state_class
        target_id = catalog.records[0].target_id
        assert len(target_id) == 25
        assert target_id.startswith("HA")
        assert len(fake_domoticz.create_calls) == 1
        assert first_position == len(first_connection.sent)

        unit = fake_domoticz.devices[target_id].Units[1]
        assert unit.Name == source_name
        assert unit.Type == expected_type
        assert unit.SubType == expected_subtype
        assert unit.SwitchType == 0
        assert unit.Options == expected_options
        assert unit.Used == 1
        assert unit.nValue == 0
        assert unit.sValue == "12.5"
        assert fake_domoticz.devices[target_id].TimedOut == 0
    finally:
        await _close_plugin_connection(
            first,
            first_websocket,
            manager,
            bridge_view,
            1,
        )

    (
        second,
        second_connection,
        second_websocket,
        second_position,
        second_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    try:
        assert second_inventory is not None
        assert len(second_inventory.pages) == 2
        assert len(second_inventory.targets) == 66
        observed_target = next(
            target
            for target in second_inventory.targets
            if target.target_id == target_id
        )
        assert observed_target.timed_out is False
        assert len(observed_target.units) == 1
        observed_unit = observed_target.units[0]
        assert observed_unit.unit == 1
        assert observed_unit.name == source_name
        assert observed_unit.type == expected_type
        assert observed_unit.subtype == expected_subtype
        assert observed_unit.switch_type == 0
        assert observed_unit.used is True
        assert observed_unit.n_value == 0
        assert observed_unit.s_value == "12.5"
        second_position = await _exchange_one_apply(
            second,
            second_connection,
            second_websocket,
            second_position,
            application,
            2,
        )
        await application.async_wait_for_calls(2)
        assert second._destination_id == destination_id
        assert second_position == len(second_connection.sent)
        assert len(fake_domoticz.create_calls) == 1
        assert fake_domoticz.devices[target_id].Units[1] is unit
        assert unit.sValue == "12.5"
    finally:
        await _close_plugin_connection(
            second,
            second_websocket,
            manager,
            bridge_view,
            2,
        )

    hass.states.async_set(
        source_entry.entity_id,
        "18.75",
        state_attributes,
    )
    (
        third,
        third_connection,
        third_websocket,
        third_position,
        third_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    try:
        assert third_inventory is not None
        before_update = next(
            target
            for target in third_inventory.targets
            if target.target_id == target_id
        )
        assert before_update.units[0].s_value == "12.5"
        third_position = await _exchange_one_apply(
            third,
            third_connection,
            third_websocket,
            third_position,
            application,
            3,
        )
        await application.async_wait_for_calls(3)

        updated_storage = HomeAssistantCatalogStorage(
            hass,
            entry_id=entry.entry_id,
            destination_id=destination_id,
        )
        updated_catalog = catalog_from_document(await updated_storage.async_load())
        assert len(updated_catalog.records) == 1
        assert updated_catalog.records[0].target_id == target_id
        assert updated_catalog.records[0].capability.value == 18.75
        assert len(fake_domoticz.create_calls) == 1
        assert fake_domoticz.devices[target_id].Units[1] is unit
        assert unit.sValue == "18.75"
        assert unit.updates[-1] == {"Log": False}
        assert third_position == len(third_connection.sent)
        assert fake_domoticz.errors == []
    finally:
        await _close_plugin_connection(
            third,
            third_websocket,
            manager,
            bridge_view,
            3,
        )

    target_count = len(fake_domoticz.devices)
    removed_device = fake_domoticz.devices.pop(target_id)
    assert removed_device.Units[1] is unit
    (
        fourth,
        fourth_connection,
        fourth_websocket,
        fourth_position,
        fourth_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    try:
        assert fourth_inventory is not None
        assert len(fourth_inventory.targets) == target_count - 1
        assert all(target.target_id != target_id for target in fourth_inventory.targets)
        (
            fourth_position,
            recreate_request,
            recreate_result,
        ) = await _exchange_observed_numeric_apply(
            fourth,
            fourth_connection,
            fourth_websocket,
            fourth_position,
        )
        assert recreate_request.action.kind.value == "update"
        assert recreate_request.action.target_id == target_id
        assert recreate_result.status is ApplyResultStatus.CONFIRMED
        assert recreate_result.target_id == target_id
        assert recreate_result.source == recreate_request.action.capability.source
        await application.async_wait_for_calls(4)

        recreated_catalog = catalog_from_document(await storage.async_load())
        assert len(recreated_catalog.records) == 1
        assert recreated_catalog.records[0].target_id == target_id
        assert not recreated_catalog.records[0].pending
        assert len(fake_domoticz.devices) == target_count
        assert len(fake_domoticz.create_calls) == 2
        replacement_device = fake_domoticz.devices[target_id]
        replacement_unit = replacement_device.Units[1]
        assert replacement_device is not removed_device
        assert replacement_unit is not unit
        assert replacement_unit.Name == source_name
        assert replacement_unit.Type == expected_type
        assert replacement_unit.SubType == expected_subtype
        assert replacement_unit.SwitchType == 0
        assert replacement_unit.Options == expected_options
        assert replacement_unit.Used == 1
        assert (replacement_unit.nValue, replacement_unit.sValue) == (0, "18.75")
        assert replacement_device.TimedOut == 0
        assert fourth_position == len(fourth_connection.sent)
    finally:
        await _close_plugin_connection(
            fourth,
            fourth_websocket,
            manager,
            bridge_view,
            4,
        )

    (
        fifth,
        fifth_connection,
        fifth_websocket,
        fifth_position,
        fifth_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    try:
        assert fifth_inventory is not None
        assert len(fifth_inventory.targets) == target_count
        (
            fifth_position,
            reassert_request,
            reassert_result,
        ) = await _exchange_observed_numeric_apply(
            fifth,
            fifth_connection,
            fifth_websocket,
            fifth_position,
        )
        assert reassert_request.action.kind.value == "update"
        assert reassert_request.action.target_id == target_id
        assert reassert_result.status is ApplyResultStatus.CONFIRMED
        assert reassert_result.target_id == target_id
        assert reassert_result.source == reassert_request.action.capability.source
        await application.async_wait_for_calls(5)
        assert len(fake_domoticz.devices) == target_count
        assert len(fake_domoticz.create_calls) == 2
        assert fake_domoticz.devices[target_id] is replacement_device
        assert replacement_device.Units[1] is replacement_unit
        assert replacement_unit.sValue == "18.75"
        assert all(
            fake_domoticz.devices[foreign_id] is foreign_device
            and foreign_device.Units == {}
            for foreign_id, foreign_device in foreign_devices.items()
        )
        assert fifth_position == len(fifth_connection.sent)
        assert fake_domoticz.errors == []
    finally:
        await _close_plugin_connection(
            fifth,
            fifth_websocket,
            manager,
            bridge_view,
            5,
        )


@pytest.mark.asyncio
async def test_real_bridge_reconciles_binary_sensor_across_reconnects(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create, update, and reassert an unavailable native binary target."""
    export_label = lr.async_get(hass).async_create(EXPORT_LABEL_NAME)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="binary-integration-entry",
        data={CONF_EXPORT_LABEL_ID: export_label.label_id},
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    source_entry = registry.async_get_or_create(
        "binary_sensor",
        "integration_test",
        "hall_motion",
        suggested_object_id="hall_motion",
        original_device_class=BinarySensorDeviceClass.MOTION,
        original_name="Hall motion",
    )
    registry.async_update_entity(
        source_entry.entity_id,
        labels={export_label.label_id},
    )
    state_attributes = {
        ATTR_DEVICE_CLASS: BinarySensorDeviceClass.MOTION,
        ATTR_FRIENDLY_NAME: "Hall motion",
    }
    hass.states.async_set(source_entry.entity_id, STATE_ON, state_attributes)

    application = _ObservedApplication(HomeAssistantExportApplication(hass))
    manager = DomoticzBridgeManager(application)
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id=entry.entry_id,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    bridge_view = _ObservedBridgeView(manager)
    hass.http.register_view(bridge_view)
    client = await hass_client_no_auth()
    endpoint = client.make_url("/")
    assert endpoint.port is not None
    plugin_module, fake_domoticz = _load_plugin(
        monkeypatch,
        address=endpoint.host,
        port=endpoint.port,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    _assert_inventory_negotiation_enabled(plugin_module)

    async def open_and_reconcile(
        expected_call: int,
        *,
        expect_apply: bool,
    ) -> tuple[
        object,
        _FakeConnection,
        object,
        int,
        _InventoryExchange,
    ]:
        opened = await _open_plugin_connection(
            plugin_module,
            fake_domoticz,
            client,
            monkeypatch,
        )
        plugin, connection, websocket, position, inventory = opened
        assert inventory is not None
        if expect_apply:
            position = await _exchange_one_apply(
                plugin,
                connection,
                websocket,
                position,
                application,
                expected_call,
            )
        await application.async_wait_for_calls(expected_call)
        return plugin, connection, websocket, position, inventory

    (
        first,
        first_connection,
        first_websocket,
        first_position,
        first_inventory,
    ) = await open_and_reconcile(1, expect_apply=True)
    destination_id = first._destination_id
    assert isinstance(destination_id, str)
    binary_storage = HomeAssistantBinaryCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    numeric_storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    try:
        assert first_inventory.targets == ()
        catalog = catalog_from_document(await binary_storage.async_load())
        assert len(catalog.records) == 1
        record = catalog.records[0]
        assert record.capability.semantic == BinarySensorDeviceClass.MOTION
        assert record.capability.value is True
        target_id = record.target_id
        assert await numeric_storage.async_load() is None
        assert len(fake_domoticz.create_calls) == 1
        assert first_position == len(first_connection.sent)

        unit = fake_domoticz.devices[target_id].Units[1]
        assert unit.Name == "Hall motion"
        assert unit.Type == 244
        assert unit.SubType == 73
        assert unit.SwitchType == 8
        assert unit.Used == 1
        assert unit.nValue == 1
        assert unit.sValue == "On"
        assert fake_domoticz.devices[target_id].TimedOut == 0
    finally:
        await _close_plugin_connection(
            first,
            first_websocket,
            manager,
            bridge_view,
            1,
        )

    (
        second,
        second_connection,
        second_websocket,
        second_position,
        second_inventory,
    ) = await open_and_reconcile(2, expect_apply=True)
    try:
        assert len(second_inventory.targets) == 1
        observed_target = second_inventory.targets[0]
        assert observed_target.target_id == target_id
        assert observed_target.timed_out is False
        assert len(observed_target.units) == 1
        observed_unit = observed_target.units[0]
        assert observed_unit.unit == 1
        assert observed_unit.name == "Hall motion"
        assert observed_unit.type == 244
        assert observed_unit.subtype == 73
        assert observed_unit.switch_type == 8
        assert observed_unit.used is True
        assert observed_unit.n_value == 1
        assert observed_unit.s_value == "On"
        assert second._destination_id == destination_id
        assert second_position == len(second_connection.sent)
        assert len(fake_domoticz.create_calls) == 1
        assert fake_domoticz.devices[target_id].Units[1] is unit
    finally:
        await _close_plugin_connection(
            second,
            second_websocket,
            manager,
            bridge_view,
            2,
        )

    hass.states.async_set(source_entry.entity_id, STATE_OFF, state_attributes)
    (
        third,
        third_connection,
        third_websocket,
        third_position,
        third_inventory,
    ) = await open_and_reconcile(3, expect_apply=True)
    try:
        assert third_inventory.targets[0].units[0].s_value == "On"
        updated_storage = HomeAssistantBinaryCatalogStorage(
            hass,
            entry_id=entry.entry_id,
            destination_id=destination_id,
        )
        updated_catalog = catalog_from_document(await updated_storage.async_load())
        assert updated_catalog.records[0].target_id == target_id
        assert updated_catalog.records[0].capability.value is False
        assert len(fake_domoticz.create_calls) == 1
        assert fake_domoticz.devices[target_id].Units[1] is unit
        assert unit.nValue == 0
        assert unit.sValue == "Off"
        assert fake_domoticz.devices[target_id].TimedOut == 0
        assert third_position == len(third_connection.sent)
    finally:
        await _close_plugin_connection(
            third,
            third_websocket,
            manager,
            bridge_view,
            3,
        )

    hass.states.async_set(
        source_entry.entity_id,
        STATE_UNAVAILABLE,
        state_attributes,
    )
    (
        fourth,
        fourth_connection,
        fourth_websocket,
        fourth_position,
        fourth_inventory,
    ) = await open_and_reconcile(4, expect_apply=True)
    try:
        assert fourth_inventory.targets[0].units[0].s_value == "Off"
        assert fourth_inventory.targets[0].timed_out is False
        unavailable_storage = HomeAssistantBinaryCatalogStorage(
            hass,
            entry_id=entry.entry_id,
            destination_id=destination_id,
        )
        unavailable_catalog = catalog_from_document(
            await unavailable_storage.async_load()
        )
        unavailable_record = unavailable_catalog.records[0]
        assert unavailable_record.target_id == target_id
        assert unavailable_record.capability.value is None
        assert unavailable_record.capability.availability.value == "unavailable"
        assert len(fake_domoticz.create_calls) == 1
        assert unit.nValue == 0
        assert unit.sValue == "Off"
        assert fake_domoticz.devices[target_id].TimedOut == 1
        assert fourth_position == len(fourth_connection.sent)
    finally:
        await _close_plugin_connection(
            fourth,
            fourth_websocket,
            manager,
            bridge_view,
            4,
        )

    # Domoticz does not persist Device.TimedOut. Simulate a plugin process
    # restart clearing it while Home Assistant still has the same unavailable
    # snapshot and catalog record.
    fake_domoticz.devices[target_id].TimedOut = 0
    (
        fifth,
        fifth_connection,
        fifth_websocket,
        fifth_position,
        fifth_inventory,
    ) = await open_and_reconcile(5, expect_apply=True)
    try:
        assert fifth_inventory.targets[0].timed_out is False
        assert fifth._destination_id == destination_id
        assert len(fake_domoticz.create_calls) == 1
        assert fake_domoticz.devices[target_id].Units[1] is unit
        assert unit.nValue == 0
        assert unit.sValue == "Off"
        assert fake_domoticz.devices[target_id].TimedOut == 1
        assert fifth_position == len(fifth_connection.sent)
    finally:
        await _close_plugin_connection(
            fifth,
            fifth_websocket,
            manager,
            bridge_view,
            5,
        )

    assert fake_domoticz.errors == []


@pytest.mark.asyncio
async def test_real_bridge_repairs_safe_target_after_rejecting_unsafe_targets(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unsafe binding cannot block repair or expose neighboring targets."""
    export_label = lr.async_get(hass).async_create(EXPORT_LABEL_NAME)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="integration-entry",
        data={CONF_EXPORT_LABEL_ID: export_label.label_id},
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    state_attributes = {
        ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY,
        ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING,
        ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR,
    }
    source_entries = []
    for index in range(3):
        source_name = f"Safety source {index}"
        source_entry = registry.async_get_or_create(
            "sensor",
            "integration_test",
            f"safety_source_{index}",
            suggested_object_id=f"safety_source_{index}",
            capabilities={ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING},
            original_device_class=SensorDeviceClass.ENERGY,
            original_name=source_name,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        registry.async_update_entity(
            source_entry.entity_id,
            labels={export_label.label_id},
        )
        hass.states.async_set(
            source_entry.entity_id,
            str(10 + index),
            {**state_attributes, ATTR_FRIENDLY_NAME: source_name},
        )
        source_entries.append(source_entry)

    application = _ObservedApplication(HomeAssistantExportApplication(hass))
    manager = DomoticzBridgeManager(application)
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id=entry.entry_id,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    bridge_view = _ObservedBridgeView(manager)
    hass.http.register_view(bridge_view)
    client = await hass_client_no_auth()
    endpoint = client.make_url("/")
    assert endpoint.port is not None
    plugin_module, fake_domoticz = _load_plugin(
        monkeypatch,
        address=endpoint.host,
        port=endpoint.port,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    _assert_inventory_negotiation_enabled(plugin_module)

    (
        first,
        first_connection,
        first_websocket,
        first_position,
        first_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    destination_id = first._destination_id
    assert isinstance(destination_id, str)
    storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    try:
        assert first_inventory is not None
        assert first_inventory.targets == ()
        for _ in source_entries:
            first_position = await _exchange_one_apply(
                first,
                first_connection,
                first_websocket,
                first_position,
                application,
                1,
            )
        await application.async_wait_for_calls(1)
        assert first_position == len(first_connection.sent)
    finally:
        await _close_plugin_connection(
            first,
            first_websocket,
            manager,
            bridge_view,
            1,
        )

    baseline_document = await storage.async_load()
    assert baseline_document is not None
    baseline_catalog = catalog_from_document(baseline_document)
    assert len(baseline_catalog.records) == 3
    incompatible_record, safe_record, sibling_record = baseline_catalog.records
    incompatible_id = incompatible_record.target_id
    safe_id = safe_record.target_id
    sibling_id = sibling_record.target_id
    assert len(fake_domoticz.create_calls) == 3

    def target_snapshot(device: _FakeDevice, unit: _FakeUnit) -> tuple[object, ...]:
        return (
            device.DeviceID,
            device.TimedOut,
            tuple(device.Units),
            unit.Unit,
            unit.Name,
            unit.Type,
            unit.SubType,
            unit.SwitchType,
            tuple(sorted(unit.Options.items())),
            unit.Used,
            unit.nValue,
            unit.sValue,
            tuple(tuple(sorted(update.items())) for update in unit.updates),
            unit.refreshes,
        )

    incompatible_device = fake_domoticz.devices[incompatible_id]
    incompatible_unit = incompatible_device.Units[1]
    incompatible_device.TimedOut = 1
    incompatible_unit.Name = "Manual incompatible name"
    incompatible_unit.Type = 244
    incompatible_unit.SubType = 73
    incompatible_unit.SwitchType = 8
    incompatible_unit.Options = {"Private": "incompatible"}
    incompatible_unit.Used = 0
    incompatible_unit.nValue = 9
    incompatible_unit.sValue = "manual-incompatible"
    incompatible_snapshot = target_snapshot(
        incompatible_device,
        incompatible_unit,
    )

    safe_device = fake_domoticz.devices[safe_id]
    safe_unit = safe_device.Units[1]
    safe_expected_name = safe_unit.Name
    safe_expected_values = (safe_unit.nValue, safe_unit.sValue)
    safe_expected_custom = safe_unit.Options["Custom"]
    safe_updates_before = len(safe_unit.updates)
    safe_refreshes_before = safe_unit.refreshes
    safe_device.TimedOut = 1
    safe_unit.Name = "Manual safe drift"
    safe_unit.Options = {"Custom": "1;wrong", "Private": "safe"}
    safe_unit.Used = 0
    safe_unit.nValue = 7
    safe_unit.sValue = "manual-safe"

    sibling_device = fake_domoticz.devices[sibling_id]
    sibling_unit = sibling_device.Units[1]
    sibling_device.TimedOut = 1
    sibling_unit.Name = "Manual sibling parent"
    sibling_unit.Options = {"Custom": "1;sibling", "Private": "unit-1"}
    sibling_unit.Used = 0
    sibling_unit.nValue = 8
    sibling_unit.sValue = "manual-sibling"
    sibling_unit_2 = _FakeUnit(
        fake_domoticz,
        Unit=2,
        Name="Manual sibling Unit 2",
        Type=243,
        Subtype=31,
        Switchtype=0,
        Options={"Custom": "1;private", "Private": "unit-2"},
        Used=0,
        nValue=6,
        sValue="manual-unit-2",
    )
    sibling_device.Units[2] = sibling_unit_2
    sibling_snapshot = target_snapshot(sibling_device, sibling_unit)
    sibling_2_snapshot = target_snapshot(sibling_device, sibling_unit_2)

    collision_name = "Manual collision source"
    collision_entry = registry.async_get_or_create(
        "sensor",
        "integration_test",
        "manual_collision_source",
        suggested_object_id="manual_collision_source",
        capabilities={ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING},
        original_device_class=SensorDeviceClass.ENERGY,
        original_name=collision_name,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    registry.async_update_entity(
        collision_entry.entity_id,
        labels={export_label.label_id},
    )
    hass.states.async_set(
        collision_entry.entity_id,
        "42",
        {**state_attributes, ATTR_FRIENDLY_NAME: collision_name},
    )
    collection = collect_export_selection(
        hass,
        instance_id=await async_get_instance_id(hass),
        label_id=export_label.label_id,
        included_kinds=frozenset({CapabilityKind.NUMERIC}),
    )
    collision_capability = next(
        capability
        for capability in collection.capabilities
        if capability.source.object_id == collision_entry.id
    )
    collision_id = derive_domoticz_target_id(collision_capability.source)
    assert collision_id not in fake_domoticz.devices
    collision_device = _FakeDevice(collision_id)
    collision_device.TimedOut = 1
    collision_unit = _FakeUnit(
        fake_domoticz,
        Unit=1,
        Name="Unrelated manual target",
        Type=243,
        Subtype=31,
        Switchtype=0,
        Options={"Custom": "1;manual", "Private": "collision"},
        Used=0,
        nValue=5,
        sValue="manual-collision",
    )
    collision_device.Units[1] = collision_unit
    fake_domoticz.devices[collision_id] = collision_device
    collision_snapshot = target_snapshot(collision_device, collision_unit)

    create_count = len(fake_domoticz.create_calls)
    target_ids = frozenset(fake_domoticz.devices)
    (
        second,
        second_connection,
        second_websocket,
        second_position,
        second_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    try:
        assert second_inventory is not None
        inventory_by_id = {
            target.target_id: target for target in second_inventory.targets
        }
        assert frozenset(inventory_by_id) == target_ids
        assert tuple(unit.unit for unit in inventory_by_id[sibling_id].units) == (1, 2)

        (
            second_position,
            rejected_request,
            rejected_result,
        ) = await _exchange_observed_numeric_apply(
            second,
            second_connection,
            second_websocket,
            second_position,
        )
        assert rejected_request.action.target_id == incompatible_id
        assert rejected_result.status is ApplyResultStatus.REJECTED
        assert rejected_result.target_id is None
        assert rejected_result.source is None

        (
            second_position,
            repaired_request,
            repaired_result,
        ) = await _exchange_observed_numeric_apply(
            second,
            second_connection,
            second_websocket,
            second_position,
        )
        assert repaired_request.action.target_id == safe_id
        assert repaired_result.status is ApplyResultStatus.CONFIRMED
        assert repaired_result.target_id == safe_id
        assert repaired_result.source == safe_record.capability.source
        await application.async_wait_for_calls(2)
        assert second_position == len(second_connection.sent)
    finally:
        await _close_plugin_connection(
            second,
            second_websocket,
            manager,
            bridge_view,
            2,
        )

    assert fake_domoticz.devices[safe_id] is safe_device
    assert safe_device.Units[1] is safe_unit
    assert safe_unit.Name == safe_expected_name
    assert (safe_unit.Type, safe_unit.SubType, safe_unit.SwitchType) == (243, 31, 0)
    assert safe_unit.Options == {
        "Custom": safe_expected_custom,
        "Private": "safe",
    }
    assert safe_unit.Used == 1
    assert (safe_unit.nValue, safe_unit.sValue) == safe_expected_values
    assert safe_device.TimedOut == 0
    assert len(safe_unit.updates) == safe_updates_before + 1
    assert safe_unit.updates[-1] == {
        "Log": False,
        "UpdateProperties": True,
        "UpdateOptions": True,
    }
    assert safe_unit.refreshes == safe_refreshes_before + 1

    assert fake_domoticz.devices[incompatible_id] is incompatible_device
    assert incompatible_device.Units[1] is incompatible_unit
    assert target_snapshot(incompatible_device, incompatible_unit) == (
        incompatible_snapshot
    )
    assert fake_domoticz.devices[sibling_id] is sibling_device
    assert sibling_device.Units[1] is sibling_unit
    assert sibling_device.Units[2] is sibling_unit_2
    assert target_snapshot(sibling_device, sibling_unit) == sibling_snapshot
    assert target_snapshot(sibling_device, sibling_unit_2) == sibling_2_snapshot
    assert fake_domoticz.devices[collision_id] is collision_device
    assert collision_device.Units[1] is collision_unit
    assert target_snapshot(collision_device, collision_unit) == collision_snapshot

    assert len(fake_domoticz.create_calls) == create_count
    assert frozenset(fake_domoticz.devices) == target_ids
    assert await storage.async_load() == baseline_document
    final_catalog = catalog_from_document(await storage.async_load())
    assert tuple(record.target_id for record in final_catalog.records) == (
        incompatible_id,
        safe_id,
        sibling_id,
    )
    assert all(not record.pending for record in final_catalog.records)
    assert final_catalog.get(collision_capability.source) is None
    assert application.completed_calls == 2
    assert fake_domoticz.errors == []


@pytest.mark.asyncio
async def test_real_bridge_continuous_export_recovers_without_duplicates(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply live deltas, reconnect for discovery, and recover the latest state."""
    export_label = lr.async_get(hass).async_create(EXPORT_LABEL_NAME)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="continuous-integration-entry",
        data={CONF_EXPORT_LABEL_ID: export_label.label_id},
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    numeric_entry = registry.async_get_or_create(
        "sensor",
        "integration_test",
        "continuous_temperature",
        suggested_object_id="continuous_temperature",
        capabilities={ATTR_STATE_CLASS: SensorStateClass.MEASUREMENT},
        original_device_class=SensorDeviceClass.TEMPERATURE,
        original_name="Continuous temperature",
        unit_of_measurement=UnitOfTemperature.CELSIUS,
    )
    binary_entry = registry.async_get_or_create(
        "binary_sensor",
        "integration_test",
        "continuous_motion",
        suggested_object_id="continuous_motion",
        original_device_class=BinarySensorDeviceClass.MOTION,
        original_name="Continuous motion",
    )
    registry.async_update_entity(
        numeric_entry.entity_id,
        labels={export_label.label_id},
    )
    registry.async_update_entity(
        binary_entry.entity_id,
        labels={export_label.label_id},
    )
    numeric_attributes = {
        ATTR_DEVICE_CLASS: SensorDeviceClass.TEMPERATURE,
        ATTR_FRIENDLY_NAME: "Continuous temperature",
        ATTR_STATE_CLASS: SensorStateClass.MEASUREMENT,
        ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.CELSIUS,
    }
    binary_attributes = {
        ATTR_DEVICE_CLASS: BinarySensorDeviceClass.MOTION,
        ATTR_FRIENDLY_NAME: "Continuous motion",
    }
    hass.states.async_set(numeric_entry.entity_id, "18.5", numeric_attributes)
    hass.states.async_set(binary_entry.entity_id, STATE_ON, binary_attributes)

    application = _ObservedApplication(HomeAssistantExportApplication(hass))
    manager = DomoticzBridgeManager(application)
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id=entry.entry_id,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    bridge_view = _ObservedBridgeView(manager)
    hass.http.register_view(bridge_view)
    client = await hass_client_no_auth()
    endpoint = client.make_url("/")
    assert endpoint.port is not None
    plugin_module, fake_domoticz = _load_plugin(
        monkeypatch,
        address=endpoint.host,
        port=endpoint.port,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    _assert_continuous_negotiation_enabled(plugin_module)
    monkeypatch.setattr(
        bridge_reconciliation_module,
        "CONTINUOUS_COALESCE_SECONDS",
        0.05,
    )
    monkeypatch.setattr(bridge_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(bridge_module, "HEARTBEAT_RESPONSE_TIMEOUT", 0.5)

    (
        first,
        first_connection,
        first_websocket,
        first_position,
        first_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    assert first_inventory is not None
    assert first_inventory.targets == ()
    first_selection = _selected_protocol(first)
    assert first_selection.supports(FEATURE_DOMOTICZ_INVENTORY_V1)
    assert first_selection.supports(FEATURE_HA_EXPORT_CONTINUOUS_V1)

    (
        first_position,
        numeric_create,
        numeric_create_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    (
        first_position,
        binary_create,
        binary_create_result,
    ) = await _exchange_observed_binary_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    await application.async_wait_for_calls(1)
    assert numeric_create.action.kind.value == "create"
    assert binary_create.action.kind.value == "create"
    assert numeric_create_result.status is ApplyResultStatus.CONFIRMED
    assert binary_create_result.status is ApplyResultStatus.CONFIRMED

    destination_id = first._destination_id
    assert isinstance(destination_id, str)
    numeric_storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    binary_storage = HomeAssistantBinaryCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    numeric_catalog = catalog_from_document(await numeric_storage.async_load())
    binary_catalog = catalog_from_document(await binary_storage.async_load())
    assert len(numeric_catalog.records) == 1
    assert len(binary_catalog.records) == 1
    numeric_target_id = numeric_catalog.records[0].target_id
    binary_target_id = binary_catalog.records[0].target_id
    numeric_device = fake_domoticz.devices[numeric_target_id]
    binary_device = fake_domoticz.devices[binary_target_id]
    numeric_unit = numeric_device.Units[1]
    binary_unit = binary_device.Units[1]
    assert len(fake_domoticz.devices) == 2
    assert len(fake_domoticz.create_calls) == 2

    numeric_updates_before = len(numeric_unit.updates)
    binary_updates_before = len(binary_unit.updates)
    await asyncio.sleep(0.03)
    hass.states.async_set(
        numeric_entry.entity_id,
        "19.0",
        numeric_attributes | {ATTR_FRIENDLY_NAME: "Discarded temperature"},
    )
    hass.states.async_set(
        numeric_entry.entity_id,
        "20.0",
        numeric_attributes | {ATTR_FRIENDLY_NAME: "Intermediate temperature"},
    )
    final_numeric_attributes = numeric_attributes | {
        ATTR_FRIENDLY_NAME: "Latest temperature"
    }
    hass.states.async_set(
        numeric_entry.entity_id,
        "22.75",
        final_numeric_attributes,
    )
    hass.states.async_set(binary_entry.entity_id, STATE_OFF, binary_attributes)
    hass.states.async_set(binary_entry.entity_id, STATE_ON, binary_attributes)
    final_binary_attributes = binary_attributes | {ATTR_FRIENDLY_NAME: "Latest motion"}
    hass.states.async_set(
        binary_entry.entity_id,
        STATE_OFF,
        final_binary_attributes,
    )
    await hass.async_block_till_done()

    burst_position = first_position
    (
        first_position,
        numeric_update,
        numeric_update_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    (
        first_position,
        binary_update,
        binary_update_result,
    ) = await _exchange_observed_binary_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert first_position >= burst_position + 3
    assert numeric_update.action.kind.value == "update"
    assert numeric_update.action.target_id == numeric_target_id
    assert numeric_update.action.capability.value == 22.75
    assert numeric_update.action.capability.name == "Latest temperature"
    assert binary_update.action.kind.value == "update"
    assert binary_update.action.target_id == binary_target_id
    assert binary_update.action.capability.value is False
    assert binary_update.action.capability.name == "Latest motion"
    assert numeric_update_result.status is ApplyResultStatus.CONFIRMED
    assert binary_update_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_unit.Name == "Latest temperature"
    assert numeric_unit.sValue == "22.75"
    assert binary_unit.Name == "Latest motion"
    assert (binary_unit.nValue, binary_unit.sValue) == (0, "Off")
    assert len(numeric_unit.updates) == numeric_updates_before + 1
    assert len(binary_unit.updates) == binary_updates_before + 1
    first_position = await _assert_no_application_payload(
        first,
        first_connection,
        first_websocket,
        first_position,
    )

    preserved_numeric_values = (numeric_unit.nValue, numeric_unit.sValue)
    hass.states.async_set(
        numeric_entry.entity_id,
        STATE_UNAVAILABLE,
        final_numeric_attributes,
    )
    await hass.async_block_till_done()
    (
        first_position,
        source_unavailable,
        source_unavailable_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert source_unavailable.action.kind.value == "mark_unavailable"
    assert source_unavailable.action.target_id == numeric_target_id
    assert source_unavailable.action.capability.availability.value == "unavailable"
    assert source_unavailable.action.stale is False
    assert source_unavailable_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_device.TimedOut == 1
    assert (numeric_unit.nValue, numeric_unit.sValue) == preserved_numeric_values
    assert fake_domoticz.devices[numeric_target_id] is numeric_device
    assert numeric_device.Units[1] is numeric_unit
    assert len(fake_domoticz.devices) == 2
    assert len(fake_domoticz.create_calls) == 2

    hass.states.async_set(
        numeric_entry.entity_id,
        "22.75",
        final_numeric_attributes,
    )
    await hass.async_block_till_done()
    (
        first_position,
        source_restored,
        source_restored_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert source_restored.action.kind.value == "update"
    assert source_restored.action.target_id == numeric_target_id
    assert source_restored.action.capability.value == 22.75
    assert source_restored_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_device.TimedOut == 0
    assert fake_domoticz.devices[numeric_target_id] is numeric_device
    assert numeric_device.Units[1] is numeric_unit
    assert len(fake_domoticz.devices) == 2
    assert len(fake_domoticz.create_calls) == 2

    hass.states.async_remove(numeric_entry.entity_id)
    await hass.async_block_till_done()
    (
        first_position,
        missing_state,
        missing_state_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert missing_state.action.kind.value == "mark_unavailable"
    assert missing_state.action.target_id == numeric_target_id
    assert missing_state.action.stale is False
    assert missing_state_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_device.TimedOut == 1

    registry_only_name = "Registry-only temperature"
    assert hass.states.get(numeric_entry.entity_id) is None
    registry.async_update_entity(
        numeric_entry.entity_id,
        name=registry_only_name,
    )
    await hass.async_block_till_done()
    assert hass.states.get(numeric_entry.entity_id) is None
    (
        first_position,
        metadata_update,
        metadata_update_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert metadata_update.action.kind.value == "mark_unavailable"
    assert metadata_update.action.target_id == numeric_target_id
    assert metadata_update.action.capability.name == registry_only_name
    assert metadata_update.action.stale is False
    assert metadata_update_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_unit.Name == registry_only_name
    assert numeric_device.TimedOut == 1
    assert fake_domoticz.devices[numeric_target_id] is numeric_device
    assert numeric_device.Units[1] is numeric_unit
    assert len(fake_domoticz.devices) == 2
    assert len(fake_domoticz.create_calls) == 2

    renamed_numeric_attributes = numeric_attributes | {
        ATTR_FRIENDLY_NAME: registry_only_name
    }
    hass.states.async_set(
        numeric_entry.entity_id,
        "22.75",
        renamed_numeric_attributes,
    )
    await hass.async_block_till_done()
    (
        first_position,
        metadata_source_restored,
        metadata_source_restored_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert metadata_source_restored.action.kind.value == "update"
    assert metadata_source_restored.action.target_id == numeric_target_id
    assert metadata_source_restored.action.capability.name == registry_only_name
    assert metadata_source_restored.action.capability.value == 22.75
    assert metadata_source_restored_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_unit.Name == registry_only_name
    assert numeric_device.TimedOut == 0
    assert fake_domoticz.devices[numeric_target_id] is numeric_device
    assert numeric_device.Units[1] is numeric_unit
    assert len(fake_domoticz.devices) == 2
    assert len(fake_domoticz.create_calls) == 2
    first_position = await _assert_no_application_payload(
        first,
        first_connection,
        first_websocket,
        first_position,
    )

    registry.async_update_entity(numeric_entry.entity_id, labels=set())
    await hass.async_block_till_done()
    (
        first_position,
        unavailable,
        unavailable_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert unavailable.action.kind.value == "mark_unavailable"
    assert unavailable.action.target_id == numeric_target_id
    assert unavailable.action.stale is True
    assert unavailable_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_device.TimedOut == 1
    assert len(fake_domoticz.create_calls) == 2
    first_position = await _assert_no_application_payload(
        first,
        first_connection,
        first_websocket,
        first_position,
    )

    registry.async_update_entity(
        numeric_entry.entity_id,
        labels={export_label.label_id},
    )
    await hass.async_block_till_done()
    (
        first_position,
        relabelled,
        relabelled_result,
    ) = await _exchange_observed_numeric_apply(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert relabelled.action.kind.value == "update"
    assert relabelled.action.target_id == numeric_target_id
    assert relabelled.action.capability.value == 22.75
    assert relabelled_result.status is ApplyResultStatus.CONFIRMED
    assert numeric_device.TimedOut == 0
    assert fake_domoticz.devices[numeric_target_id] is numeric_device
    assert numeric_device.Units[1] is numeric_unit
    assert len(fake_domoticz.devices) == 2
    assert len(fake_domoticz.create_calls) == 2
    first_position = await _assert_no_application_payload(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    relabelled_storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    relabelled_catalog = catalog_from_document(await relabelled_storage.async_load())
    assert relabelled_catalog.records[0].target_id == numeric_target_id
    assert relabelled_catalog.records[0].stale is False

    discovered_entry = registry.async_get_or_create(
        "sensor",
        "integration_test",
        "continuous_discovered_energy",
        suggested_object_id="continuous_discovered_energy",
        capabilities={ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING},
        original_device_class=SensorDeviceClass.ENERGY,
        original_name="Discovered energy",
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    discovered_attributes = {
        ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY,
        ATTR_FRIENDLY_NAME: "Discovered energy",
        ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING,
        ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR,
    }
    hass.states.async_set(discovered_entry.entity_id, "7.5", discovered_attributes)
    await hass.async_block_till_done()
    current_numeric_storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    current_binary_storage = HomeAssistantBinaryCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    catalogs_before_discovery = (
        await current_numeric_storage.async_load(),
        await current_binary_storage.async_load(),
    )
    update_counts_before_discovery = {
        target_id: len(device.Units[1].updates)
        for target_id, device in fake_domoticz.devices.items()
    }
    availability_before_discovery = {
        target_id: device.TimedOut
        for target_id, device in fake_domoticz.devices.items()
    }
    registry.async_update_entity(
        discovered_entry.entity_id,
        labels={export_label.label_id},
    )
    await hass.async_block_till_done()
    first_position = await _wait_for_controlled_close(
        first,
        first_connection,
        first_websocket,
        first_position,
    )
    assert first_position == len(first_connection.sent)
    await bridge_view.async_wait_for_requests(1)
    first.onStop()
    assert await manager.async_active_session_count() == 0
    assert len(fake_domoticz.devices) == 2
    assert len(fake_domoticz.create_calls) == 2
    post_close_numeric_storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    post_close_binary_storage = HomeAssistantBinaryCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    assert (
        await post_close_numeric_storage.async_load(),
        await post_close_binary_storage.async_load(),
    ) == catalogs_before_discovery
    assert {
        target_id: len(device.Units[1].updates)
        for target_id, device in fake_domoticz.devices.items()
    } == update_counts_before_discovery
    assert {
        target_id: device.TimedOut
        for target_id, device in fake_domoticz.devices.items()
    } == availability_before_discovery

    recovered_attributes = numeric_attributes | {
        ATTR_FRIENDLY_NAME: "Recovered latest temperature"
    }
    hass.states.async_set(
        numeric_entry.entity_id,
        "41.5",
        recovered_attributes,
    )
    await hass.async_block_till_done()

    (
        second,
        second_connection,
        second_websocket,
        second_position,
        second_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    assert second_inventory is not None
    assert len(second_inventory.targets) == 2
    reconnect_numeric_requests = []
    reconnect_numeric_results = []
    for _index in range(2):
        (
            second_position,
            reconnect_request,
            reconnect_result,
        ) = await _exchange_observed_numeric_apply(
            second,
            second_connection,
            second_websocket,
            second_position,
        )
        reconnect_numeric_requests.append(reconnect_request)
        reconnect_numeric_results.append(reconnect_result)
    (
        second_position,
        reconnect_binary,
        reconnect_binary_result,
    ) = await _exchange_observed_binary_apply(
        second,
        second_connection,
        second_websocket,
        second_position,
    )
    await application.async_wait_for_calls(2)

    create_request = next(
        request
        for request in reconnect_numeric_requests
        if request.action.kind.value == "create"
    )
    recovered_request = next(
        request
        for request in reconnect_numeric_requests
        if request.action.target_id == numeric_target_id
    )
    assert recovered_request.action.kind.value == "update"
    assert recovered_request.action.capability.value == 41.5
    assert recovered_request.action.capability.name == "Recovered latest temperature"
    assert reconnect_binary.action.kind.value == "update"
    assert reconnect_binary.action.target_id == binary_target_id
    assert all(
        result.status is ApplyResultStatus.CONFIRMED
        for result in reconnect_numeric_results
    )
    assert reconnect_binary_result.status is ApplyResultStatus.CONFIRMED
    assert len(fake_domoticz.devices) == 3
    assert len(fake_domoticz.create_calls) == 3
    assert numeric_unit.sValue == "41.5"
    assert numeric_unit.Name == "Recovered latest temperature"

    recovered_numeric_storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    recovered_numeric_catalog = catalog_from_document(
        await recovered_numeric_storage.async_load()
    )
    assert len(recovered_numeric_catalog.records) == 2
    discovered_record = recovered_numeric_catalog.get(
        create_request.action.capability.source
    )
    assert discovered_record is not None
    assert (
        discovered_record.target_id
        == reconnect_numeric_results[
            reconnect_numeric_requests.index(create_request)
        ].target_id
    )
    assert discovered_record.pending is False
    second_position = await _assert_no_application_payload(
        second,
        second_connection,
        second_websocket,
        second_position,
    )
    assert second_position == len(second_connection.sent)
    assert fake_domoticz.errors == []

    await _close_plugin_connection(
        second,
        second_websocket,
        manager,
        bridge_view,
        2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_side", ("home_assistant", "plugin"))
@pytest.mark.parametrize("legacy_generation", ("pre_inventory", "pre_continuous"))
async def test_real_bridge_preserves_mixed_v2_fallback(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    legacy_side: str,
    legacy_generation: str,
) -> None:
    """Either older peer keeps its mutually supported connect-time behavior."""
    export_label = lr.async_get(hass).async_create(EXPORT_LABEL_NAME)
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="mixed-v2-integration-entry",
        data={CONF_EXPORT_LABEL_ID: export_label.label_id},
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    source_name = "Mixed-version energy"
    source_entry = registry.async_get_or_create(
        "sensor",
        "integration_test",
        "mixed_version_energy",
        suggested_object_id="mixed_version_energy",
        capabilities={ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING},
        original_device_class=SensorDeviceClass.ENERGY,
        original_name=source_name,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    registry.async_update_entity(
        source_entry.entity_id,
        labels={export_label.label_id},
    )
    hass.states.async_set(
        source_entry.entity_id,
        "12.5",
        {
            ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY,
            ATTR_FRIENDLY_NAME: source_name,
            ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING,
            ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR,
        },
    )

    application = _ObservedApplication(HomeAssistantExportApplication(hass))
    manager = DomoticzBridgeManager(application)
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id=entry.entry_id,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    bridge_view = _ObservedBridgeView(manager)
    hass.http.register_view(bridge_view)
    client = await hass_client_no_auth()
    endpoint = client.make_url("/")
    assert endpoint.port is not None
    plugin_module, fake_domoticz = _load_plugin(
        monkeypatch,
        address=endpoint.host,
        port=endpoint.port,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert FEATURE_DOMOTICZ_INVENTORY_V1 in bridge_module.SUPPORTED_V2_FEATURES
    assert (
        FEATURE_DOMOTICZ_INVENTORY_V1
        in plugin_module.wire_protocol.SUPPORTED_V2_FEATURES
    )
    removed_features = {FEATURE_HA_EXPORT_CONTINUOUS_V1}
    if legacy_generation == "pre_inventory":
        removed_features.add(FEATURE_DOMOTICZ_INVENTORY_V1)
    if legacy_side == "home_assistant":
        monkeypatch.setattr(
            bridge_module,
            "SUPPORTED_V2_FEATURES",
            tuple(
                feature
                for feature in bridge_module.SUPPORTED_V2_FEATURES
                if feature not in removed_features
            ),
        )
    else:
        monkeypatch.setattr(
            plugin_module.wire_protocol,
            "SUPPORTED_V2_FEATURES",
            tuple(
                feature
                for feature in plugin_module.wire_protocol.SUPPORTED_V2_FEATURES
                if feature not in removed_features
            ),
        )

    inventory_expected = legacy_generation == "pre_continuous"
    assert (FEATURE_DOMOTICZ_INVENTORY_V1 in bridge_module.SUPPORTED_V2_FEATURES) is (
        legacy_side == "plugin" or inventory_expected
    )
    assert (
        FEATURE_DOMOTICZ_INVENTORY_V1
        in plugin_module.wire_protocol.SUPPORTED_V2_FEATURES
    ) is (legacy_side == "home_assistant" or inventory_expected)
    assert (FEATURE_HA_EXPORT_CONTINUOUS_V1 in bridge_module.SUPPORTED_V2_FEATURES) is (
        legacy_side == "plugin"
    )
    assert (
        FEATURE_HA_EXPORT_CONTINUOUS_V1
        in plugin_module.wire_protocol.SUPPORTED_V2_FEATURES
    ) is (legacy_side == "home_assistant")

    (
        first,
        first_connection,
        first_websocket,
        first_position,
        first_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    destination_id = first._destination_id
    assert isinstance(destination_id, str)
    storage = HomeAssistantCatalogStorage(
        hass,
        entry_id=entry.entry_id,
        destination_id=destination_id,
    )
    try:
        selection = _selected_protocol(first)
        assert selection.supports(FEATURE_DOMOTICZ_INVENTORY_V1) is inventory_expected
        assert not selection.supports(FEATURE_HA_EXPORT_CONTINUOUS_V1)
        if inventory_expected:
            assert first_inventory is not None
            assert first_inventory.targets == ()
        else:
            assert first_inventory is None
        first_position, request, result = await _exchange_observed_numeric_apply(
            first,
            first_connection,
            first_websocket,
            first_position,
        )
        await application.async_wait_for_calls(1)

        assert request.action.kind.value == "create"
        assert result.status is ApplyResultStatus.CONFIRMED
        assert result.target_id is not None
        assert result.source == request.action.capability.source
        assert first_position == len(first_connection.sent)
    finally:
        await _close_plugin_connection(
            first,
            first_websocket,
            manager,
            bridge_view,
            1,
        )

    baseline_document = await storage.async_load()
    assert baseline_document is not None
    baseline_catalog = catalog_from_document(baseline_document)
    assert len(baseline_catalog.records) == 1
    record = baseline_catalog.records[0]
    target_id = record.target_id
    assert target_id == result.target_id
    assert not record.pending
    assert len(fake_domoticz.devices) == 1
    assert len(fake_domoticz.create_calls) == 1
    device = fake_domoticz.devices[target_id]
    unit = device.Units[1]

    drifted_name = "Manual mixed-version drift"
    unit.Name = drifted_name
    (
        second,
        second_connection,
        second_websocket,
        second_position,
        second_inventory,
    ) = await _open_plugin_connection(
        plugin_module,
        fake_domoticz,
        client,
        monkeypatch,
    )
    try:
        second_selection = _selected_protocol(second)
        assert (
            second_selection.supports(FEATURE_DOMOTICZ_INVENTORY_V1)
            is inventory_expected
        )
        assert not second_selection.supports(FEATURE_HA_EXPORT_CONTINUOUS_V1)
        if inventory_expected:
            assert second_inventory is not None
            assert tuple(target.target_id for target in second_inventory.targets) == (
                target_id,
            )
            (
                second_position,
                reconnect_request,
                reconnect_result,
            ) = await _exchange_observed_numeric_apply(
                second,
                second_connection,
                second_websocket,
                second_position,
            )
            assert reconnect_request.action.kind.value == "update"
            assert reconnect_request.action.target_id == target_id
            assert reconnect_result.status is ApplyResultStatus.CONFIRMED
        else:
            assert second_inventory is None
        await application.async_wait_for_calls(2)
        hass.states.async_set(
            source_entry.entity_id,
            "13.75",
            {
                ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY,
                ATTR_FRIENDLY_NAME: "Ignored same-session update",
                ATTR_STATE_CLASS: SensorStateClass.TOTAL_INCREASING,
                ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR,
            },
        )
        await hass.async_block_till_done()
        second_position = await _assert_no_application_payload(
            second,
            second_connection,
            second_websocket,
            second_position,
        )

        assert second_position == len(second_connection.sent)
        assert len(fake_domoticz.devices) == 1
        assert len(fake_domoticz.create_calls) == 1
        assert fake_domoticz.devices[target_id] is device
        assert device.Units[1] is unit
        assert unit.Name == (source_name if inventory_expected else drifted_name)
        assert unit.sValue == "12.5"
        assert await storage.async_load() == baseline_document
        assert fake_domoticz.errors == []
    finally:
        await _close_plugin_connection(
            second,
            second_websocket,
            manager,
            bridge_view,
            2,
        )
