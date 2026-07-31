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

from aiohttp import WSMsgType  # noqa: E402
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
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)
from pytest_homeassistant_custom_component.typing import (  # noqa: E402
    ClientSessionGenerator,
)

from custom_components.domoticz_sync import bridge as bridge_module  # noqa: E402
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
from custom_components.domoticz_sync.core.catalog import (  # noqa: E402
    catalog_from_document,
)
from custom_components.domoticz_sync.core.protocol import (  # noqa: E402
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
    FEATURE_DOMOTICZ_INVENTORY_V1,
    InventoryResult,
    InventoryTarget,
    ProtocolSelection,
    assemble_inventory_results,
    canonical_json_loads,
    generate_link_id,
    generate_pairing_key,
    parse_inventory_request,
    parse_inventory_result,
    verify_envelope,
)

ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


@dataclass(frozen=True)
class _InventoryExchange:
    """One authenticated, fully assembled inventory observed by the test."""

    request_id: str
    pages: tuple[InventoryResult, ...]
    targets: tuple[InventoryTarget, ...]


def _enable_inventory_negotiation(
    monkeypatch: pytest.MonkeyPatch,
    plugin_module: ModuleType,
) -> None:
    """Opt this test into the dormant Phase 5.2 inventory feature."""
    features = tuple(
        sorted(
            {
                *bridge_module.SUPPORTED_V2_FEATURES,
                FEATURE_DOMOTICZ_INVENTORY_V1,
            }
        )
    )
    monkeypatch.setattr(bridge_module, "SUPPORTED_V2_FEATURES", features)
    monkeypatch.setattr(
        plugin_module.wire_protocol,
        "SUPPORTED_V2_FEATURES",
        features,
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


async def _exchange_complete_inventory(
    plugin: object,
    connection: _FakeConnection,
    websocket: object,
    send_position: int,
) -> tuple[int, _InventoryExchange]:
    """Exchange and independently verify one complete signed inventory."""
    selection = _selected_protocol(plugin)
    assert selection.supports(FEATURE_DOMOTICZ_INVENTORY_V1)

    request_text = await _receive_text(websocket)
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
        self.attempted_calls = 0
        self.completed_calls = 0
        self.failure_type_chains: list[tuple[str, ...]] = []
        self.failure_frames: list[tuple[tuple[str, str, int], ...]] = []

    async def async_connected(
        self,
        session: BridgeApplicationSession,
    ) -> None:
        try:
            await self._application.async_connected(session)
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
                self.attempted_calls += 1
                self.failure_type_chains.append(tuple(type_chain))
                self.failure_frames.append(frames)
                self._condition.notify_all()
            raise
        async with self._condition:
            self.attempted_calls += 1
            self.completed_calls += 1
            self._condition.notify_all()

    async def async_wait_for_calls(self, expected: int) -> None:
        """Wait until the real application has completed enough sessions."""
        async with asyncio.timeout(2):
            async with self._condition:
                await self._condition.wait_for(lambda: self.attempted_calls >= expected)
        assert self.completed_calls >= expected, (
            "bridge application failed with exception types "
            f"{self.failure_type_chains[-1]} at {self.failure_frames[-1]}"
        )


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
        payload = await _receive_text(websocket)
    except AssertionError:
        await application.async_wait_for_calls(expected_application_call)
        raise
    plugin.onMessage(connection, {"Payload": payload})
    result, send_position = _next_text_payload(connection, send_position)
    await websocket.send_str(result)
    return send_position


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
    manager = DomoticzBridgeManager()
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
    _enable_inventory_negotiation(monkeypatch, plugin_module)

    # Force a two-page snapshot so the test observes that no apply is sent
    # after page 1. Empty containers are valid hardware-scoped inventory and
    # remain unrelated to the HA catalog.
    for index in range(65):
        target_id = f"FOREIGN{index:018d}"
        fake_domoticz.devices[target_id] = _FakeDevice(target_id)

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
    _enable_inventory_negotiation(monkeypatch, plugin_module)

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
