"""Tests for the root Domoticz companion plugin."""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MISSING = object()


class FakeConnection:
    """Small callback-native Domoticz connection stand-in."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.sent = []
        self.connecting = False
        self.connected = False
        self.disconnected = False

    def Connect(self):
        self.connecting = True

    def Connected(self):
        return self.connected

    def Connecting(self):
        return self.connecting

    def Send(self, document):
        self.sent.append(document)

    def Disconnect(self):
        self.connected = False
        self.connecting = False
        self.disconnected = True


class FakeUnit:
    """In-memory DomoticzEx unit with observable persistence calls."""

    def __init__(self, domoticz, **kwargs):
        self._domoticz = domoticz
        self._device_id = kwargs.get("DeviceID")
        self.Name = kwargs.get("Name")
        self.Unit = kwargs.get("Unit", 1)
        self.Type = kwargs.get("Type", 0)
        self.SubType = kwargs.get("Subtype", kwargs.get("SubType", 0))
        self.SwitchType = kwargs.get("Switchtype", kwargs.get("SwitchType", 0))
        self.Options = dict(kwargs.get("Options", {}))
        self.Used = kwargs.get("Used", 0)
        self.nValue = kwargs.get("nValue", 0)
        self.sValue = kwargs.get("sValue", "")
        self.updates = []
        self.refreshes = 0
        self.deleted = False

    def Update(self, **kwargs):
        self.updates.append(dict(kwargs))

    def Refresh(self):
        self.refreshes += 1
        if self._domoticz.corrupt_refreshes:
            self.sValue = "not-persisted"

    def Delete(self):
        self.deleted = True
        device = self._domoticz.devices.get(self._device_id)
        if device is not None:
            device.Units.pop(self.Unit, None)


class FakeDevice:
    """Container matching DomoticzEx's extended device model."""

    def __init__(self, domoticz, device_id):
        self._domoticz = domoticz
        self.DeviceID = device_id
        self._timed_out = 0
        self.Units = {}

    @property
    def TimedOut(self):
        return self._timed_out

    @TimedOut.setter
    def TimedOut(self, value):
        self._timed_out = value


class FakeUnitCreator:
    """Deferred DomoticzEx Unit creator."""

    def __init__(self, domoticz, kwargs):
        self._domoticz = domoticz
        self._kwargs = kwargs

    def Create(self):
        self._domoticz.create_calls.append(dict(self._kwargs))
        unit = FakeUnit(self._domoticz, **self._kwargs)
        if self._domoticz.persist_creates:
            device = self._domoticz.devices.setdefault(
                unit._device_id,
                FakeDevice(self._domoticz, unit._device_id),
            )
            device.Units[unit.Unit] = unit
        return unit


class FakeDomoticz(ModuleType):
    """DomoticzEx module with in-memory configuration and connections."""

    def __init__(self):
        super().__init__("DomoticzEx")
        self.configuration = {}
        self.configuration_writes = []
        self.connections = []
        self.devices = {}
        self.create_calls = []
        self.persist_creates = True
        self.corrupt_refreshes = False
        self.logs = []
        self.statuses = []
        self.errors = []
        self.heartbeat_seconds = None

    def Configuration(self, config=MISSING):
        if config is not MISSING:
            self.configuration = dict(config)
            self.configuration_writes.append(dict(config))
        return dict(self.configuration)

    def Connection(self, **kwargs):
        connection = FakeConnection(**kwargs)
        self.connections.append(connection)
        return connection

    def Heartbeat(self, seconds):
        self.heartbeat_seconds = seconds

    def Unit(self, **kwargs):
        return FakeUnitCreator(self, kwargs)

    def Log(self, message):
        self.logs.append(message)

    def Status(self, message):
        self.statuses.append(message)

    def Error(self, message):
        self.errors.append(message)


@pytest.fixture
def loaded_plugin(monkeypatch):
    """Load plugin.py with an isolated DomoticzEx implementation."""
    fake_domoticz = FakeDomoticz()
    monkeypatch.setitem(sys.modules, "DomoticzEx", fake_domoticz)
    monkeypatch.delitem(sys.modules, "core", raising=False)
    module_name = "domoticz_plugin_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.Parameters = {
        "Address": "homeassistant.local",
        "Port": "8123",
        "Mode1": "WSS",
        "Mode2": module.wire_protocol.generate_link_id(),
        "Mode3": module.wire_protocol.generate_pairing_key(),
        "HomeFolder": str(ROOT),
    }
    module.Devices = fake_domoticz.devices
    return module, fake_domoticz


def _upgrade_response(
    connection,
    *,
    extensions=None,
    valid_accept=True,
    subprotocol=MISSING,
):
    request = connection.sent[0]
    key = request["Headers"]["Sec-WebSocket-Key"]
    accept = base64.b64encode(
        hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    ).decode("ascii")
    if not valid_accept:
        accept = "invalid"
    headers = {
        "Upgrade": "websocket",
        "Connection": "keep-alive, Upgrade",
        "Sec-WebSocket-Accept": accept,
    }
    if extensions is not None:
        headers["Sec-WebSocket-Extensions"] = extensions
    if subprotocol is MISSING:
        subprotocol = request["Headers"]["Sec-WebSocket-Protocol"].split(",")[0].strip()
    if subprotocol is not None:
        headers["Sec-WebSocket-Protocol"] = subprotocol
    return {"Status": "101", "Headers": headers}


def _start_and_upgrade(module, *, subprotocol=MISSING):
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection
    connection.connecting = False
    connection.connected = True
    plugin.onConnect(connection, 0, "ignored")
    plugin.onMessage(
        connection,
        _upgrade_response(connection, subprotocol=subprotocol),
    )
    return plugin, connection


def _legacy_v2_features(protocol):
    """Return the feature set advertised by a pre-inventory v2 peer."""
    return (
        protocol.FEATURE_HA_EXPORT_BINARY_V1,
        protocol.FEATURE_HA_EXPORT_NUMERIC_V1,
    )


def _complete_handshake(
    module,
    plugin,
    connection,
    *,
    server_features=None,
):
    protocol = module.wire_protocol
    hello_document = protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    if plugin._protocol_version == protocol.PROTOCOL_VERSION_V2:
        hello = protocol.parse_v2_hello(hello_document)
        context = protocol.make_v2_handshake_context(
            hello,
            protocol.generate_nonce(),
            server_protocols=protocol.SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
            server_features=(
                _legacy_v2_features(protocol)
                if server_features is None
                else server_features
            ),
        )
        challenge = protocol.build_v2_challenge(
            module.Parameters["Mode3"],
            context,
        )
    else:
        hello = protocol.parse_hello(hello_document)
        context = protocol.make_handshake_context(hello, protocol.generate_nonce())
        challenge = protocol.build_challenge(module.Parameters["Mode3"], context)
    challenge_text = protocol.canonical_json_dumps(challenge)

    midpoint = len(challenge_text) // 2
    plugin.onMessage(
        connection,
        {"Payload": challenge_text[:midpoint], "Finish": False},
    )
    sent_before_final_fragment = len(connection.sent)
    plugin.onMessage(
        connection,
        {"Payload": challenge_text[midpoint:], "Finish": True},
    )
    assert len(connection.sent) == sent_before_final_fragment + 1

    authenticate = protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    if plugin._protocol_version == protocol.PROTOCOL_VERSION_V2:
        protocol.verify_v2_authenticate(
            module.Parameters["Mode3"],
            context,
            authenticate,
        )
        session_key = protocol.derive_v2_session_key(
            module.Parameters["Mode3"],
            context,
        )
        ready = protocol.build_v2_ready(session_key, context)
        session_id = protocol.derive_v2_session_id(session_key, context)
    else:
        protocol.verify_authenticate(
            module.Parameters["Mode3"],
            context,
            authenticate,
        )
        session_key = protocol.derive_session_key(
            module.Parameters["Mode3"],
            context,
        )
        ready = protocol.build_ready(session_key, context)
        session_id = protocol.derive_session_id(session_key, context)
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(ready)},
    )
    initial_message = protocol.verify_envelope(
        session_key,
        protocol.canonical_json_loads(connection.sent[-1]["Payload"]),
        protocol_version=plugin._protocol_version,
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=0,
    )
    if plugin._protocol_version == protocol.PROTOCOL_VERSION_V2:
        protocol.parse_application_ready(context.selection, initial_message.payload)
        ready_payload = protocol.build_application_ready(context.selection)
    else:
        assert initial_message.payload == {"type": "inventory", "targets": []}
        ready_payload = {"type": "ready"}

    application_ready = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=1,
        payload=ready_payload,
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(application_ready)},
    )
    assert plugin.phase == module.PHASE_READY
    return session_key, session_id


def _numeric_action(
    protocol,
    *,
    kind="create",
    target_id=None,
    value=12.5,
    availability="available",
    stale=False,
    name="Outdoor temperature",
    unit="deg C",
    semantic=None,
    state_class=None,
):
    """Build one representative neutral numeric action."""
    source = protocol.SourceIdentity(
        system="home_assistant",
        instance_id="instance-1",
        object_id="sensor.outdoor_temperature",
        capability_id="state",
    )
    capability = protocol.Capability(
        source=source,
        kind=protocol.CapabilityKind.NUMERIC,
        name=name,
        value=value,
        availability=protocol.Availability(availability),
        semantic=semantic,
        unit=unit,
        state_class=state_class,
    )
    return protocol.ReconciliationAction(
        kind=protocol.ReconciliationActionKind(kind),
        capability=capability,
        target_id=target_id,
        stale=stale,
    )


def _binary_action(
    protocol,
    *,
    kind="create",
    target_id=None,
    value=True,
    availability="available",
    stale=False,
    name="Back door",
    semantic="door",
):
    """Build one representative neutral passive binary action."""
    source = protocol.SourceIdentity(
        system="home_assistant",
        instance_id="instance-1",
        object_id="binary_sensor.back_door",
        capability_id="state",
    )
    capability = protocol.Capability(
        source=source,
        kind=protocol.CapabilityKind.BINARY,
        name=name,
        value=value,
        availability=protocol.Availability(availability),
        semantic=semantic,
    )
    return protocol.ReconciliationAction(
        kind=protocol.ReconciliationActionKind(kind),
        capability=capability,
        target_id=target_id,
        stale=stale,
    )


@pytest.mark.parametrize(
    (
        "semantic",
        "unit",
        "value",
        "expected_type",
        "expected_subtype",
        "expected_n_value",
        "expected_s_value",
    ),
    [
        ("temperature", "°C", 20.25, 80, 5, 0, "20.25"),
        ("temperature", "°F", 68, 80, 5, 0, "20.0"),
        ("temperature", "K", 293.15, 80, 5, 0, "20.0"),
        ("temperature", "celsius", 10, 80, 5, 0, "10"),
        ("temperature", "fahrenheit", 50, 80, 5, 0, "10.0"),
        ("humidity", "%", 24.4, 81, 1, 24, "2"),
        ("humidity", "%", 24.5, 81, 1, 25, "1"),
        ("humidity", "%", 60.5, 81, 1, 61, "3"),
        (None, "%", 42.5, 243, 6, 0, "42.5"),
        (None, "percent", 43.5, 243, 6, 0, "43.5"),
        ("atmospheric_pressure", "Pa", 101325, 243, 26, 0, "1013.25;5"),
        ("atmospheric_pressure", "hpa", 1013, 243, 26, 0, "1013;5"),
        ("pressure", "kPa", 250, 243, 9, 0, "2.5"),
        ("voltage", "mV", 2500, 243, 8, 0, "2.5"),
        ("voltage", "volt", 230, 243, 8, 0, "230"),
        ("current", "mA", 1250, 243, 23, 0, "1.25"),
        ("power", "kW", 1.5, 248, 1, 0, "1500.0"),
        ("power", "watt", 1500, 248, 1, 0, "1500"),
        ("illuminance", "lx", 325.5, 246, 1, 0, "325.5"),
        ("illuminance", "lux", 326.5, 246, 1, 0, "326.5"),
        ("distance", "m", 1.25, 243, 27, 0, "125.0"),
        ("weight", "g", 1250, 93, 1, 0, "1.25"),
        ("sound_pressure", "dB", 49.5, 243, 24, 0, "50"),
        ("irradiance", "W/m²", 1200.5, 243, 2, 0, "1200.5"),
        ("irradiance", "BTU/(h⋅ft²)", 1, 243, 2, 0, "3.154590745"),
        ("carbon_dioxide", "ppm", 812.5, 249, 1, 813, ""),
        (None, "UV index", 6.25, 87, 1, 0, "6.25;0"),
    ],
)
def test_native_numeric_profile_selection_and_encoding(
    loaded_plugin,
    semantic,
    unit,
    value,
    expected_type,
    expected_subtype,
    expected_n_value,
    expected_s_value,
):
    """Each supported Home Assistant gauge has one exact Domoticz codec."""
    module, _domoticz = loaded_plugin
    action = _numeric_action(
        module.wire_protocol,
        semantic=semantic,
        unit=unit,
        value=value,
        state_class="measurement",
    )

    profile = module._target_profile(action.capability)
    encoded = profile.encoder(value, unit)

    assert profile.type_id == expected_type
    assert profile.subtype == expected_subtype
    assert profile.switch_type == 0
    assert profile.manages_options is False
    assert encoded == (expected_n_value, expected_s_value)


@pytest.mark.parametrize(
    ("semantic", "unit", "state_class"),
    [
        ("temperature", "°C", "total"),
        ("power", "W", "total_increasing"),
        ("aqi", None, "measurement"),
        ("energy", "kWh", "total_increasing"),
        ("volume_flow_rate", "m³/h", "measurement"),
        ("temperature", "mystery", "measurement"),
        (None, "°C", "measurement"),
    ],
)
def test_ambiguous_or_counter_numeric_values_fall_back_to_custom(
    loaded_plugin,
    semantic,
    unit,
    state_class,
):
    """Unsupported semantics remain lossless Custom Sensors."""
    module, _domoticz = loaded_plugin
    action = _numeric_action(
        module.wire_protocol,
        semantic=semantic,
        unit=unit,
        state_class=state_class,
    )

    profile = module._target_profile(action.capability)

    assert profile.type_id == 243
    assert profile.subtype == 31
    assert profile.switch_type == 0
    assert profile.manages_options is True


@pytest.mark.parametrize(
    ("semantic", "expected_switch_type"),
    [
        ("battery", 0),
        ("battery_charging", 0),
        ("carbon_monoxide", 0),
        ("cold", 0),
        ("connectivity", 0),
        ("door", 11),
        ("garage_door", 11),
        ("gas", 0),
        ("heat", 0),
        ("light", 0),
        ("lock", 20),
        ("moisture", 0),
        ("motion", 8),
        ("moving", 0),
        ("occupancy", 0),
        ("opening", 2),
        ("plug", 0),
        ("power", 0),
        ("presence", 0),
        ("problem", 0),
        ("running", 0),
        ("safety", 0),
        ("smoke", 5),
        ("sound", 0),
        ("tamper", 0),
        ("update", 0),
        ("vibration", 0),
        ("window", 2),
        (None, 0),
        ("future_device_class", 0),
    ],
)
def test_all_binary_device_classes_have_a_safe_passive_profile(
    loaded_plugin,
    semantic,
    expected_switch_type,
):
    """Known, absent, and future classes select an explicit safe switch."""
    module, _domoticz = loaded_plugin
    action = _binary_action(module.wire_protocol, semantic=semantic)

    profile = module._binary_target_profile(action.capability)

    assert profile.type_id == 244
    assert profile.subtype == 73
    assert profile.switch_type == expected_switch_type
    assert profile.manages_options is False
    assert profile.encoder(True, None) == (1, "On")
    assert profile.encoder(False, None) == (0, "Off")


def test_binary_profile_rejects_non_boolean_values(loaded_plugin):
    """The Domoticz codec does not coerce integers or strings to switches."""
    module, _domoticz = loaded_plugin
    action = _binary_action(module.wire_protocol)
    profile = module._binary_target_profile(action.capability)

    with pytest.raises(module.DomoticzApplyError):
        profile.encoder(1, None)


def _send_apply(
    module,
    plugin,
    connection,
    session_key,
    session_id,
    *,
    request_id,
    action=None,
    payload=None,
):
    """Send one signed apply payload and parse its signed response."""
    protocol = module.wire_protocol
    if payload is None:
        payload = protocol.build_apply(
            plugin._protocol_selection,
            request_id,
            action,
        )
    previous_out_sequence = plugin._out_sequence
    envelope = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=plugin._in_sequence + 1,
        payload=payload,
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(envelope)},
    )
    response = protocol.verify_envelope(
        session_key,
        protocol.canonical_json_loads(connection.sent[-1]["Payload"]),
        protocol_version=plugin._protocol_version,
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=previous_out_sequence,
    )
    return protocol.parse_apply_result(
        plugin._protocol_selection,
        response.payload,
    )


def _send_binary_apply(
    module,
    plugin,
    connection,
    session_key,
    session_id,
    *,
    request_id,
    action,
):
    """Send one signed passive binary request and parse its response."""
    protocol = module.wire_protocol
    payload = protocol.build_binary_apply(
        plugin._protocol_selection,
        request_id,
        action,
    )
    previous_out_sequence = plugin._out_sequence
    envelope = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=plugin._in_sequence + 1,
        payload=payload,
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(envelope)},
    )
    response = protocol.verify_envelope(
        session_key,
        protocol.canonical_json_loads(connection.sent[-1]["Payload"]),
        protocol_version=plugin._protocol_version,
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=previous_out_sequence,
    )
    return protocol.parse_binary_apply_result(
        plugin._protocol_selection,
        response.payload,
    )


def _inventory_features(_monkeypatch, protocol):
    """Return the active feature set used by inventory runtime tests."""
    assert protocol.FEATURE_DOMOTICZ_INVENTORY_V1 in (protocol.SUPPORTED_V2_FEATURES)
    return protocol.SUPPORTED_V2_FEATURES


def _continuous_features(monkeypatch, protocol):
    """Enable the not-yet-default continuous contract for focused tests."""
    features = tuple(
        sorted(
            (
                *protocol.SUPPORTED_V2_FEATURES,
                protocol.FEATURE_HA_EXPORT_CONTINUOUS_V1,
            )
        )
    )
    monkeypatch.setattr(protocol, "SUPPORTED_V2_FEATURES", features)
    return features


def _send_inventory_request(
    module,
    plugin,
    connection,
    session_key,
    session_id,
    *,
    request_id="inventory-1",
):
    """Send one signed inventory request and parse every queued result page."""
    protocol = module.wire_protocol
    payload = protocol.build_inventory_request(
        plugin._protocol_selection,
        request_id,
    )
    envelope = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=plugin._in_sequence + 1,
        payload=payload,
    )
    sent_position = len(connection.sent)
    last_sequence = plugin._out_sequence
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(envelope)},
    )

    results = []
    for sent in connection.sent[sent_position:]:
        verified = protocol.verify_envelope(
            session_key,
            protocol.canonical_json_loads(sent["Payload"]),
            protocol_version=plugin._protocol_version,
            expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=last_sequence,
        )
        last_sequence = verified.sequence
        results.append(
            protocol.parse_inventory_result(
                plugin._protocol_selection,
                verified.payload,
            )
        )
    return tuple(results)


def _start_inventory_session(module, monkeypatch):
    """Start one authenticated v2 session with inventory negotiated."""
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    return plugin, connection, session_key, session_id


def test_inventory_request_returns_complete_deterministic_hardware_snapshot(
    loaded_plugin,
    monkeypatch,
):
    """The plugin snapshots outer DeviceIDs, empty parents, and ordered Units."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )

    domoticz.devices["alpha-parent"] = FakeDevice(domoticz, "alpha-parent")
    module.Domoticz.Unit(
        Name="Second unit",
        DeviceID="zulu-parent",
        Unit=2,
        Type=243,
        Subtype=31,
        Switchtype=0,
        Options={"Custom": "1;ppm", "Calibration": "7"},
        Used=0,
        nValue=-1,
        sValue="12.5",
    ).Create()
    module.Domoticz.Unit(
        Name="First unit",
        DeviceID="zulu-parent",
        Unit=1,
        Type=244,
        Subtype=73,
        Switchtype=8,
        Used=1,
        nValue=0,
        sValue="Off",
    ).Create()
    domoticz.devices["zulu-parent"].TimedOut = 1
    assert not hasattr(domoticz.devices["zulu-parent"].Units[1], "DeviceID")

    results = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    assert results == (
        protocol.InventoryResult(
            request_id="inventory-1",
            status=protocol.InventoryResultStatus.CONFIRMED,
            page=1,
            complete=True,
            targets=(
                protocol.InventoryTarget(
                    target_id="alpha-parent",
                    timed_out=False,
                    units=(),
                ),
                protocol.InventoryTarget(
                    target_id="zulu-parent",
                    timed_out=True,
                    units=(
                        protocol.InventoryUnit(
                            unit=1,
                            name="First unit",
                            type=244,
                            subtype=73,
                            switch_type=8,
                            used=True,
                            n_value=0,
                            s_value="Off",
                            custom_option=None,
                            has_other_options=False,
                        ),
                        protocol.InventoryUnit(
                            unit=2,
                            name="Second unit",
                            type=243,
                            subtype=31,
                            switch_type=0,
                            used=False,
                            n_value=-1,
                            s_value="12.5",
                            custom_option="1;ppm",
                            has_other_options=True,
                        ),
                    ),
                ),
            ),
        ),
    )
    assert (
        protocol.assemble_inventory_results(
            plugin._protocol_selection,
            "inventory-1",
            results,
        )
        == results[0].targets
    )


def test_empty_inventory_has_one_confirmed_terminal_page(
    loaded_plugin,
    monkeypatch,
):
    """An empty hardware registry is distinct from an inventory rejection."""
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )

    assert _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    ) == (
        protocol.InventoryResult(
            request_id="inventory-1",
            status=protocol.InventoryResultStatus.CONFIRMED,
            page=1,
            complete=True,
            targets=(),
        ),
    )


def test_inventory_request_pages_one_complete_snapshot(
    loaded_plugin,
    monkeypatch,
):
    """A large snapshot is sent in ordered, contiguous, bounded pages."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    target_count = protocol.MAX_INVENTORY_TARGETS_PER_PAGE + 1
    for index in reversed(range(target_count)):
        target_id = f"parent-{index:03d}"
        domoticz.devices[target_id] = FakeDevice(domoticz, target_id)

    results = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    assert tuple(result.page for result in results) == (1, 2)
    assert tuple(result.complete for result in results) == (False, True)
    assert tuple(len(result.targets) for result in results) == (
        protocol.MAX_INVENTORY_TARGETS_PER_PAGE,
        1,
    )
    assembled = protocol.assemble_inventory_results(
        plugin._protocol_selection,
        "inventory-1",
        results,
    )
    assert tuple(target.target_id for target in assembled) == tuple(
        f"parent-{index:03d}" for index in range(target_count)
    )


def test_inventory_write_gate_opens_only_after_every_page_is_sent(
    loaded_plugin,
    monkeypatch,
):
    """The terminal inventory send must return before writes become eligible."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    target_count = protocol.MAX_INVENTORY_TARGETS_PER_PAGE + 1
    for index in range(target_count):
        target_id = f"gate-parent-{index:03d}"
        domoticz.devices[target_id] = FakeDevice(domoticz, target_id)
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    gate_states = []
    original_send = plugin._send_signed

    def recording_send(payload):
        original_send(payload)
        if payload.get("type") == "inventory_result":
            gate_states.append(plugin._inventory_confirmed)
            with pytest.raises(module.DomoticzApplyError):
                plugin._require_inventory_write_gate()

    monkeypatch.setattr(plugin, "_send_signed", recording_send)

    results = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    assert len(results) == 2
    assert gate_states == [False, False]
    assert plugin._inventory_confirmed
    plugin._require_inventory_write_gate()


def test_terminal_inventory_send_failure_leaves_write_gate_closed(
    loaded_plugin,
    monkeypatch,
):
    """A partial result transmission never authorizes later mutations."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    target_count = protocol.MAX_INVENTORY_TARGETS_PER_PAGE + 1
    for index in range(target_count):
        target_id = f"failed-send-parent-{index:03d}"
        domoticz.devices[target_id] = FakeDevice(domoticz, target_id)
    plugin, _connection, _session_key, _session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    original_send = plugin._send_signed
    sent_pages = []

    def fail_terminal_send(payload):
        if payload.get("type") == "inventory_result":
            sent_pages.append(payload["page"])
            if payload["complete"]:
                raise RuntimeError
        original_send(payload)

    monkeypatch.setattr(plugin, "_send_signed", fail_terminal_send)

    with pytest.raises(RuntimeError):
        plugin._handle_inventory_request(
            protocol.build_inventory_request(
                plugin._protocol_selection,
                "failed-terminal-send",
            )
        )

    assert sent_pages == [1, 2]
    assert not plugin._inventory_confirmed
    with pytest.raises(module.DomoticzApplyError):
        plugin._require_inventory_write_gate()


def test_inventory_request_pages_snapshot_by_canonical_byte_size(
    loaded_plugin,
    monkeypatch,
):
    """Payload bytes can split a page before its target-count ceiling."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    target_count = (
        protocol.MAX_INVENTORY_PAYLOAD_BYTES // protocol.MAX_INVENTORY_S_VALUE_BYTES
    ) + 1
    for index in range(target_count):
        target_id = f"large-parent-{index:03d}"
        device = FakeDevice(domoticz, target_id)
        device.Units[1] = FakeUnit(
            domoticz,
            DeviceID=target_id,
            Unit=1,
            Name="Large unit",
            sValue="v" * protocol.MAX_INVENTORY_S_VALUE_BYTES,
        )
        domoticz.devices[target_id] = device

    results = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    assert len(results) > 1
    assert all(
        len(result.targets) < protocol.MAX_INVENTORY_TARGETS_PER_PAGE
        for result in results
    )
    assembled = protocol.assemble_inventory_results(
        plugin._protocol_selection,
        "inventory-1",
        results,
    )
    assert len(assembled) == target_count


def test_inventory_target_larger_than_one_page_is_safely_rejected(
    loaded_plugin,
    monkeypatch,
):
    """One atomic parent that cannot fit a page never produces partial data."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    target_id = "oversized-parent"
    device = FakeDevice(domoticz, target_id)
    unit_count = (
        protocol.MAX_INVENTORY_PAYLOAD_BYTES // protocol.MAX_INVENTORY_S_VALUE_BYTES
    ) + 1
    for unit_number in range(1, unit_count + 1):
        device.Units[unit_number] = FakeUnit(
            domoticz,
            DeviceID=target_id,
            Unit=unit_number,
            Name="Large unit",
            sValue="v" * protocol.MAX_INVENTORY_S_VALUE_BYTES,
        )
    domoticz.devices[target_id] = device

    results = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    assert results == (
        protocol.InventoryResult(
            request_id="inventory-1",
            status=protocol.InventoryResultStatus.REJECTED,
            page=1,
            complete=True,
            targets=(),
        ),
    )


def test_inventory_pages_are_prebuilt_before_any_confirmed_page_is_sent(
    loaded_plugin,
    monkeypatch,
):
    """A later page build failure produces only one sanitized rejection."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    for index in reversed(range(protocol.MAX_INVENTORY_TARGETS_PER_PAGE + 1)):
        target_id = f"parent-{index:03d}"
        domoticz.devices[target_id] = FakeDevice(domoticz, target_id)

    original_builder = protocol.build_inventory_result

    def fail_second_confirmed_page(selection, result):
        if (
            result.status is protocol.InventoryResultStatus.CONFIRMED
            and result.page == 2
        ):
            raise protocol.ProtocolFormatError("invalid protocol message")
        return original_builder(selection, result)

    monkeypatch.setattr(
        protocol,
        "build_inventory_result",
        fail_second_confirmed_page,
    )

    results = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    assert results == (
        protocol.InventoryResult(
            request_id="inventory-1",
            status=protocol.InventoryResultStatus.REJECTED,
            page=1,
            complete=True,
            targets=(),
        ),
    )


def test_invalid_domoticz_inventory_is_rejected_without_value_disclosure(
    loaded_plugin,
    monkeypatch,
):
    """Invalid local fields never become partial results or log details."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    secret_marker = "inventory-private-value"
    module.Domoticz.Unit(
        Name=secret_marker,
        DeviceID="invalid-parent",
        Unit=1,
        Type=244,
        Subtype=73,
        Switchtype=0,
        Used=1,
        nValue=0,
        sValue=secret_marker,
    ).Create()
    domoticz.devices["invalid-parent"].Units[1].Used = 2

    results = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    assert results[0].status is protocol.InventoryResultStatus.REJECTED
    assert results[0].targets == ()
    logs = "\n".join(domoticz.logs + domoticz.statuses + domoticz.errors)
    assert secret_marker not in logs


@pytest.mark.parametrize("continuous", [False, True])
def test_second_inventory_request_in_one_session_is_a_protocol_violation(
    loaded_plugin,
    monkeypatch,
    continuous,
):
    """A completed snapshot cannot be replaced later in the same session."""
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = (
        _continuous_features(monkeypatch, protocol)
        if continuous
        else _inventory_features(monkeypatch, protocol)
    )
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    repeated = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=plugin._in_sequence + 1,
        payload=protocol.build_inventory_request(
            plugin._protocol_selection,
            "inventory-2",
        ),
    )

    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(repeated)},
    )

    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert connection.sent[-1]["Operation"] == "Close"


def test_continuous_session_accepts_sequential_catalog_delta_applies(
    loaded_plugin,
    monkeypatch,
):
    """One confirmed inventory can gate multiple ordered live deltas."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _continuous_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    inventory = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    baseline_created = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="create-baseline-target",
        action=_numeric_action(protocol, value=1.0, name="Initial name"),
    )
    updated = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="update-live-target",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=baseline_created.target_id,
            value=2.0,
            name="Changed name",
        ),
    )

    assert inventory[0].targets == ()
    assert baseline_created.status is protocol.ApplyResultStatus.CONFIRMED
    assert updated.status is protocol.ApplyResultStatus.CONFIRMED
    unit = domoticz.devices[baseline_created.target_id].Units[1]
    assert unit.Name == "Changed name"
    assert unit.sValue == "2.0"
    assert plugin.phase == module.PHASE_READY
    assert plugin._inventory_confirmed


def test_inventory_request_without_negotiated_feature_closes_without_result(
    loaded_plugin,
):
    """An inventory request cannot be invoked by an older v2 peer."""
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=_legacy_v2_features(protocol),
    )
    assert not plugin._protocol_selection.supports(
        protocol.FEATURE_DOMOTICZ_INVENTORY_V1
    )
    request = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=plugin._in_sequence + 1,
        payload={
            "schema": 1,
            "type": "inventory_request",
            "request_id": "inventory-1",
        },
    )
    sent_position = len(connection.sent)

    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(request)},
    )

    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert connection.sent[sent_position:] == [connection.sent[-1]]
    assert connection.sent[-1]["Operation"] == "Close"
    assert "Payload" not in connection.sent[-1]


def test_inventory_request_guard_resets_after_reconnect(
    loaded_plugin,
    monkeypatch,
):
    """A fresh authenticated session may request its own single snapshot."""
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    features = _inventory_features(monkeypatch, protocol)
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=features,
    )
    first = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    assert first[0].status is protocol.InventoryResultStatus.CONFIRMED
    assert plugin._inventory_requested

    plugin.onDisconnect(connection)
    assert not plugin._inventory_requested
    plugin.onHeartbeat()
    reconnected = plugin.connection
    reconnected.connecting = False
    reconnected.connected = True
    plugin.onConnect(reconnected, 0, "ignored")
    plugin.onMessage(reconnected, _upgrade_response(reconnected))
    next_key, next_session_id = _complete_handshake(
        module,
        plugin,
        reconnected,
        server_features=features,
    )
    blocked = _send_apply(
        module,
        plugin,
        reconnected,
        next_key,
        next_session_id,
        request_id="reconnected-before-inventory",
        action=_numeric_action(protocol),
    )

    second = _send_inventory_request(
        module,
        plugin,
        reconnected,
        next_key,
        next_session_id,
        request_id="inventory-2",
    )

    assert blocked.status is protocol.ApplyResultStatus.REJECTED
    assert second[0].status is protocol.InventoryResultStatus.CONFIRMED
    assert second[0].request_id == "inventory-2"


@pytest.mark.parametrize("capability_kind", ["numeric", "binary"])
def test_inventory_selected_apply_waits_for_confirmed_snapshot(
    loaded_plugin,
    monkeypatch,
    capability_kind,
):
    """Neither application message can write before confirmed inventory."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    if capability_kind == "numeric":
        action = _numeric_action(protocol)
        send = _send_apply
    else:
        action = _binary_action(protocol)
        send = _send_binary_apply

    blocked = send(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id=f"{capability_kind}-before-inventory",
        action=action,
    )
    inventory = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    allowed = send(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id=f"{capability_kind}-after-inventory",
        action=action,
    )

    assert blocked.status is protocol.ApplyResultStatus.REJECTED
    assert inventory[-1].status is protocol.InventoryResultStatus.CONFIRMED
    assert allowed.status is protocol.ApplyResultStatus.CONFIRMED
    assert len(domoticz.create_calls) == 1


def test_rejected_inventory_keeps_application_writes_gated(
    loaded_plugin,
    monkeypatch,
):
    """A rejected snapshot never turns the inventory safety gate green."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    module.Devices = None
    inventory = _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    module.Devices = domoticz.devices

    blocked = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="after-rejected-inventory",
        action=_numeric_action(protocol),
    )

    assert inventory[-1].status is protocol.InventoryResultStatus.REJECTED
    assert blocked.status is protocol.ApplyResultStatus.REJECTED
    assert domoticz.create_calls == []
    assert domoticz.devices == {}


def test_inventory_create_rechecks_parent_absence_after_unit_factory(
    loaded_plugin,
    monkeypatch,
):
    """A parent appearing during CREATE preparation is never adopted."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    action = _numeric_action(protocol)
    target_id = module._device_id_for_source(action.capability.source)
    original_factory = domoticz.Unit
    collision = FakeUnit(
        domoticz,
        DeviceID=target_id,
        Unit=1,
        Name="Unrelated target",
        Type=243,
        Subtype=31,
        Options={"Custom": "1;private"},
    )

    def racing_factory(**kwargs):
        creator = original_factory(**kwargs)
        device = FakeDevice(domoticz, target_id)
        device.Units[1] = collision
        domoticz.devices[target_id] = device
        return creator

    monkeypatch.setattr(domoticz, "Unit", racing_factory)

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="create-parent-race",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert domoticz.create_calls == []
    assert domoticz.devices[target_id].Units[1] is collision
    assert collision.Name == "Unrelated target"
    assert collision.Options == {"Custom": "1;private"}


def test_inventory_create_rejects_an_existing_empty_parent(
    loaded_plugin,
    monkeypatch,
):
    """An unbound CREATE cannot claim even an empty hardware container."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    action = _numeric_action(protocol)
    target_id = module._device_id_for_source(action.capability.source)
    empty_parent = FakeDevice(domoticz, target_id)
    domoticz.devices[target_id] = empty_parent
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="create-empty-parent",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert domoticz.devices[target_id] is empty_parent
    assert empty_parent.Units == {}
    assert domoticz.create_calls == []


def test_inventory_bound_update_rejects_sibling_added_after_snapshot(
    loaded_plugin,
    monkeypatch,
):
    """A new sibling unit invalidates an otherwise owned Unit 1 binding."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    action = _numeric_action(protocol)
    target_id = module._device_id_for_source(action.capability.source)
    module.Domoticz.Unit(
        Name="Original",
        DeviceID=target_id,
        Unit=1,
        Type=243,
        Subtype=31,
        Switchtype=0,
        Options={"Custom": "1;deg C"},
        Used=1,
        nValue=0,
        sValue="4",
    ).Create()
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    device = domoticz.devices[target_id]
    unit = device.Units[1]
    device.Units[2] = FakeUnit(
        domoticz,
        DeviceID=target_id,
        Unit=2,
        Name="Unrelated sibling",
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="sibling-race",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=target_id,
            value=19,
            name="Changed",
        ),
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert unit.Name == "Original"
    assert unit.sValue == "4"
    assert unit.updates == []
    assert set(device.Units) == {1, 2}


def test_inventory_bound_update_repairs_an_empty_parent(
    loaded_plugin,
    monkeypatch,
):
    """An exact catalog binding may recreate Unit 1 in an empty parent."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    action = _numeric_action(protocol)
    target_id = module._device_id_for_source(action.capability.source)
    parent = FakeDevice(domoticz, target_id)
    domoticz.devices[target_id] = parent
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="repair-empty-parent",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=target_id,
            value=19.25,
        ),
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert domoticz.devices[target_id] is parent
    assert set(parent.Units) == {1}
    assert parent.Units[1].sValue == "19.25"


@pytest.mark.parametrize(
    ("empty_parent", "stale", "expected_status", "expected_creates"),
    [
        (False, False, "confirmed", 1),
        (False, True, "rejected", 0),
        (True, False, "confirmed", 1),
        (True, True, "rejected", 0),
    ],
)
def test_inventory_absent_or_empty_unavailable_current_differs_from_stale(
    loaded_plugin,
    monkeypatch,
    empty_parent,
    stale,
    expected_status,
    expected_creates,
):
    """Current unavailable targets are restored; stale missing ones stay gone."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    source_action = _numeric_action(protocol)
    target_id = module._device_id_for_source(source_action.capability.source)
    original_parent = None
    if empty_parent:
        original_parent = FakeDevice(domoticz, target_id)
        domoticz.devices[target_id] = original_parent
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id=f"missing-unavailable-{stale}",
        action=_numeric_action(
            protocol,
            kind="mark_unavailable",
            target_id=target_id,
            value=None,
            availability="unavailable",
            stale=stale,
        ),
    )

    assert result.status.value == expected_status
    assert len(domoticz.create_calls) == expected_creates
    if not stale:
        device = domoticz.devices[target_id]
        unit = device.Units[1]
        if original_parent is not None:
            assert device is original_parent
        assert (unit.nValue, unit.sValue) == (0, "")
        assert device.TimedOut == 1
    elif original_parent is not None:
        assert domoticz.devices[target_id] is original_parent
        assert original_parent.Units == {}
    else:
        assert target_id not in domoticz.devices


def test_inventory_repairs_mutable_custom_state_and_retains_other_options(
    loaded_plugin,
    monkeypatch,
):
    """Owned mutable fields converge without replacing unmanaged options."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    source_action = _numeric_action(protocol)
    target_id = module._device_id_for_source(source_action.capability.source)
    module.Domoticz.Unit(
        Name="Old name",
        DeviceID=target_id,
        Unit=1,
        Type=243,
        Subtype=31,
        Switchtype=0,
        Options={"Custom": "1;old", "Calibration": "keep"},
        Used=0,
        nValue=7,
        sValue="old",
    ).Create()
    device = domoticz.devices[target_id]
    device.TimedOut = 1
    unit = device.Units[1]
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="repair-custom-drift",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=target_id,
            value=18,
            name="Garden temperature",
            unit="C",
        ),
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert device.Units[1] is unit
    assert unit.Name == "Garden temperature"
    assert unit.Used == 1
    assert (unit.nValue, unit.sValue) == (0, "18")
    assert device.TimedOut == 0
    assert unit.Options == {"Custom": "1;C", "Calibration": "keep"}


def test_inventory_native_repair_leaves_options_untouched(
    loaded_plugin,
    monkeypatch,
):
    """Native profile options are outside the plugin's repair ownership."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    source_action = _numeric_action(
        protocol,
        semantic="temperature",
        unit="°C",
    )
    target_id = module._device_id_for_source(source_action.capability.source)
    module.Domoticz.Unit(
        Name="Old name",
        DeviceID=target_id,
        Unit=1,
        Type=80,
        Subtype=5,
        Switchtype=0,
        Options={"Calibration": "keep", "Native": "private"},
        Used=0,
        nValue=0,
        sValue="-1",
    ).Create()
    device = domoticz.devices[target_id]
    device.TimedOut = 1
    unit = device.Units[1]
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="repair-native-options",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=target_id,
            semantic="temperature",
            unit="°C",
            value=20,
            name="Current name",
        ),
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert unit.Name == "Current name"
    assert unit.Used == 1
    assert unit.sValue == "20"
    assert device.TimedOut == 0
    assert unit.Options == {"Calibration": "keep", "Native": "private"}


def test_inventory_incompatible_profile_is_left_untouched(
    loaded_plugin,
    monkeypatch,
):
    """Inventory ownership never permits retyping a mismatched Unit 1."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    action = _numeric_action(protocol)
    target_id = module._device_id_for_source(action.capability.source)
    module.Domoticz.Unit(
        Name="Do not replace",
        DeviceID=target_id,
        Unit=1,
        Type=244,
        Subtype=73,
        Switchtype=0,
        Options={"Private": "keep"},
        Used=0,
        nValue=9,
        sValue="private",
    ).Create()
    unit = domoticz.devices[target_id].Units[1]
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="incompatible-profile",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=target_id,
            value=19,
            name="Must not change",
        ),
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert domoticz.devices[target_id].Units[1] is unit
    assert unit.Name == "Do not replace"
    assert (unit.Type, unit.SubType, unit.SwitchType) == (244, 73, 0)
    assert unit.Options == {"Private": "keep"}
    assert (unit.Used, unit.nValue, unit.sValue) == (0, 9, "private")
    assert unit.updates == []
    assert not unit.deleted


def test_inventory_rechecks_complete_unit_keys_after_create(
    loaded_plugin,
    monkeypatch,
):
    """A sibling appearing during Create prevents later target mutation."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    original_factory = domoticz.Unit

    def racing_factory(**kwargs):
        creator = original_factory(**kwargs)
        original_create = creator.Create

        def create_with_sibling():
            created = original_create()
            device = domoticz.devices[kwargs["DeviceID"]]
            device.Units[2] = FakeUnit(
                domoticz,
                DeviceID=kwargs["DeviceID"],
                Unit=2,
                Name="Racing sibling",
            )
            return created

        creator.Create = create_with_sibling
        return creator

    monkeypatch.setattr(domoticz, "Unit", racing_factory)

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="sibling-after-create",
        action=_numeric_action(protocol),
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    target_id = module._device_id_for_source(
        _numeric_action(protocol).capability.source
    )
    assert set(domoticz.devices[target_id].Units) == {1, 2}
    assert domoticz.devices[target_id].Units[1].updates == []


def test_inventory_rechecks_complete_unit_keys_after_refresh(
    loaded_plugin,
    monkeypatch,
):
    """A sibling exposed by Refresh prevents a false confirmation."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    source_action = _numeric_action(protocol)
    target_id = module._device_id_for_source(source_action.capability.source)
    module.Domoticz.Unit(
        Name="Outdoor temperature",
        DeviceID=target_id,
        Unit=1,
        Type=243,
        Subtype=31,
        Switchtype=0,
        Options={"Custom": "1;deg C"},
        Used=1,
        nValue=0,
        sValue="12.5",
    ).Create()
    device = domoticz.devices[target_id]
    unit = device.Units[1]
    plugin, connection, session_key, session_id = _start_inventory_session(
        module,
        monkeypatch,
    )
    _send_inventory_request(
        module,
        plugin,
        connection,
        session_key,
        session_id,
    )
    original_refresh = unit.Refresh

    def refresh_with_sibling():
        original_refresh()
        device.Units[2] = FakeUnit(
            domoticz,
            DeviceID=target_id,
            Unit=2,
            Name="Racing sibling",
        )

    monkeypatch.setattr(unit, "Refresh", refresh_with_sibling)

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="sibling-after-refresh",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=target_id,
        ),
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert set(device.Units) == {1, 2}


def test_plugin_metadata_supports_direct_repository_clone():
    source = (ROOT / "plugin.py").read_text(encoding="utf-8")
    metadata_text = ast.get_docstring(ast.parse(source))
    manifest = json.loads(
        (ROOT / "custom_components/domoticz_sync/manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert metadata_text is not None
    metadata = ET.fromstring(metadata_text)
    assert metadata.tag == "plugin"
    assert 'key="HADomoticzSync"' in source
    assert metadata.attrib["version"] == manifest["version"]
    assert 'field="Address"' in source
    assert 'field="Port"' in source
    assert 'field="Mode1"' in source
    assert '<option label="WebSocket (WS)" value="WS" default="true"/>' in source
    assert '<option label="Secure WebSocket (WSS)" value="WSS"/>' in source
    assert 'field="Mode2"' in source
    assert 'field="Mode3"' in source
    assert 'label="Pairing key"' in source
    assert 'password="true"' in source
    assert 'field="Username"' not in source
    assert 'field="Password"' not in source
    assert "custom_components" in source


def test_release_automation_updates_plugin_metadata():
    """Release Please keeps the Domoticz and Home Assistant versions aligned."""
    source = (ROOT / "plugin.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "custom_components/domoticz_sync/manifest.json").read_text(
            encoding="utf-8"
        )
    )
    release_manifest = json.loads(
        (ROOT / ".release-please-manifest.json").read_text(encoding="utf-8")
    )
    release_config = json.loads(
        (ROOT / "release-please-config.json").read_text(encoding="utf-8")
    )
    extra_files = release_config["packages"]["."]["extra-files"]
    metadata_text = ast.get_docstring(ast.parse(source))
    assert metadata_text is not None
    plugin_version = ET.fromstring(metadata_text).attrib["version"]
    project_version_match = re.search(
        r'(?m)^version = "([^"]+)"$',
        project,
    )
    assert project_version_match is not None
    release_blocks = re.findall(
        r"<!-- x-release-please-start-version -->\n"
        r"(.*?)"
        r"<!-- x-release-please-end -->",
        readme,
        flags=re.DOTALL,
    )
    readme_versions = {
        version
        for block in release_blocks
        for version in re.findall(r"\bv(\d+\.\d+\.\d+)\b", block)
    }

    assert "# x-release-please-start-version" in source
    assert "# x-release-please-end" in source
    assert {"type": "generic", "path": "plugin.py"} in extra_files
    assert readme.count("x-release-please-start-version") == 2
    assert readme.count("x-release-please-end") == 2
    assert {"type": "generic", "path": "README.md"} in extra_files
    assert {
        plugin_version,
        manifest["version"],
        project_version_match.group(1),
        release_manifest["."],
    } == {plugin_version}
    assert readme_versions == {plugin_version}


def test_plugin_import_loads_protocol_without_core_package(loaded_plugin):
    module, _domoticz = loaded_plugin

    assert "core" not in sys.modules
    assert module.wire_protocol.__name__.startswith("_ha_domoticz_sync_wire_protocol_")
    assert (
        Path(module.wire_protocol.__file__).resolve()
        == (
            ROOT / "custom_components" / "domoticz_sync" / "core" / "protocol.py"
        ).resolve()
    )


def test_start_persists_and_reuses_destination_uuid(loaded_plugin):
    module, domoticz = loaded_plugin

    first = module.DomoticzSyncPlugin()
    first.onStart()
    destination_id = first._destination_id
    second = module.DomoticzSyncPlugin()
    second.onStart()

    assert second._destination_id == destination_id
    assert len(domoticz.configuration_writes) == 1
    assert domoticz.heartbeat_seconds == 5
    assert domoticz.connections[0].kwargs == {
        "Name": "Home Assistant Sync",
        "Transport": "TCP/IP",
        "Protocol": "WSS",
        "Address": "homeassistant.local",
        "Port": "8123",
    }


def test_upgrade_suppresses_compression_and_validates_response(loaded_plugin):
    module, _domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection
    plugin.onConnect(connection, 0, "ignored")

    request = connection.sent[0]
    assert request["URL"] == "/api/domoticz_sync/websocket"
    assert request["Headers"]["Sec-WebSocket-Extensions"] is None
    assert request["Headers"]["Sec-WebSocket-Protocol"] == (
        module.wire_protocol.WEBSOCKET_SUBPROTOCOL_V2
    )
    assert "Authorization" not in request["Headers"]

    plugin.onMessage(
        connection,
        _upgrade_response(connection, extensions="permessage-deflate"),
    )

    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert len(connection.sent) == 2


def test_upgrade_rejects_invalid_websocket_accept(loaded_plugin):
    module, _domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection
    plugin.onConnect(connection, 0, "ignored")

    plugin.onMessage(
        connection,
        _upgrade_response(connection, valid_accept=False),
    )

    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert not any(
        "Payload" in document and "hello" in str(document["Payload"])
        for document in connection.sent
    )


@pytest.mark.parametrize(
    "selected_protocol",
    [
        "",
        "ha-domoticz-sync.v2 ",
        "HA-DOMOTICZ-SYNC.V2",
        "ha-domoticz-sync.v2, other",
        "unknown.example",
        2,
    ],
)
def test_upgrade_rejects_non_exact_or_unknown_protocol_selection(
    loaded_plugin,
    selected_protocol,
):
    module, _domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection
    plugin.onConnect(connection, 0, "ignored")

    plugin.onMessage(
        connection,
        _upgrade_response(connection, subprotocol=selected_protocol),
    )

    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert not any("Payload" in message for message in connection.sent)


def test_upgrade_rejects_duplicate_protocol_selection_headers(loaded_plugin):
    module, _domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection
    plugin.onConnect(connection, 0, "ignored")
    response = _upgrade_response(connection)
    response["Headers"]["sec-websocket-protocol"] = (
        module.wire_protocol.WEBSOCKET_SUBPROTOCOL_V2
    )

    plugin.onMessage(connection, response)

    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert not any("Payload" in message for message in connection.sent)


def test_v2_hello_repeats_http_selection_offer_and_features(loaded_plugin):
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)

    hello = protocol.parse_v2_hello(
        protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    )

    assert hello.client_protocols == protocol.SUPPORTED_WEBSOCKET_SUBPROTOCOLS
    assert hello.selected_protocol == protocol.WEBSOCKET_SUBPROTOCOL_V2
    assert hello.client_features == protocol.SUPPORTED_V2_FEATURES
    assert plugin._protocol_version == protocol.PROTOCOL_VERSION_V2


def test_v2_mutual_handshake_and_signed_ping_pong(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=protocol.SUPPORTED_V2_FEATURES,
    )

    assert domoticz.statuses == [
        "Authenticated Home Assistant connection is ready; "
        "protocol=ha-domoticz-sync.v2; "
        "features=domoticz-inventory.v1,ha-export.binary.v1,"
        "ha-export.numeric.v1."
    ]

    server_ping_id = protocol.generate_nonce()
    inbound_ping = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=2,
        payload={"type": "ping", "id": server_ping_id},
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(inbound_ping)},
    )
    outbound_pong = protocol.verify_envelope(
        session_key,
        protocol.canonical_json_loads(connection.sent[-1]["Payload"]),
        protocol_version=plugin._protocol_version,
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=1,
    )

    assert outbound_pong.payload == {"type": "pong", "id": server_ping_id}

    control_payload = b"transport-ping"
    plugin.onMessage(
        connection,
        {"Operation": "Ping", "Payload": control_payload},
    )
    assert connection.sent[-1]["Operation"] == "Pong"
    assert connection.sent[-1]["Payload"] == control_payload

    for _ in range(module._PING_INTERVAL_TICKS):
        plugin.onHeartbeat()
    outbound_ping = protocol.verify_envelope(
        session_key,
        protocol.canonical_json_loads(connection.sent[-1]["Payload"]),
        protocol_version=plugin._protocol_version,
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=2,
    )
    assert outbound_ping.payload["type"] == "ping"
    protocol.validate_nonce(outbound_ping.payload["id"])

    inbound_pong = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=3,
        payload={"type": "pong", "id": outbound_ping.payload["id"]},
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(inbound_pong)},
    )
    assert plugin._pending_ping_id is None


def test_v2_pre_inventory_peer_keeps_catalog_only_apply_behavior(loaded_plugin):
    """A rolling upgrade keeps common exports live without an inventory gate."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=_legacy_v2_features(protocol),
    )

    assert domoticz.statuses == [
        "Authenticated Home Assistant connection is ready; "
        "protocol=ha-domoticz-sync.v2; "
        "features=ha-export.binary.v1,ha-export.numeric.v1."
    ]
    assert not plugin._inventory_is_selected()

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="legacy-peer-create-1",
        action=_numeric_action(protocol),
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert len(domoticz.create_calls) == 1


def test_v1_fallback_is_heartbeat_only_and_never_applies(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module, subprotocol=None)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    ping_id = protocol.generate_nonce()
    legacy_ping = protocol.sign_envelope(
        session_key,
        protocol_version=protocol.PROTOCOL_VERSION,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=2,
        payload={"type": "ping", "id": ping_id},
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(legacy_ping)},
    )
    pong = protocol.verify_envelope(
        session_key,
        protocol.canonical_json_loads(connection.sent[-1]["Payload"]),
        protocol_version=protocol.PROTOCOL_VERSION,
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=1,
    )
    assert pong.payload == {"type": "pong", "id": ping_id}
    sent_before_apply = len(connection.sent)

    legacy_apply = protocol.sign_envelope(
        session_key,
        protocol_version=protocol.PROTOCOL_VERSION,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=3,
        payload={"type": "apply"},
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(legacy_apply)},
    )

    assert plugin._protocol_selection is None
    assert domoticz.devices == {}
    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert len(connection.sent) == sent_before_apply + 1
    assert connection.sent[-1]["Operation"] == "Close"
    assert any("v1 compatibility mode" in message for message in domoticz.statuses)


def test_v2_without_numeric_feature_does_not_parse_or_apply(
    loaded_plugin,
    monkeypatch,
):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=(),
    )
    assert plugin._protocol_selection.features == ()
    parse_calls = []

    def record_parse(*args, **kwargs):
        parse_calls.append((args, kwargs))
        raise AssertionError

    monkeypatch.setattr(protocol, "parse_apply", record_parse)
    apply = protocol.sign_envelope(
        session_key,
        protocol_version=protocol.PROTOCOL_VERSION_V2,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=2,
        payload={"schema": 1, "type": "apply"},
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(apply)},
    )

    assert parse_calls == []
    assert domoticz.devices == {}
    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected


def test_v2_numeric_only_peer_does_not_parse_or_apply_binary(
    loaded_plugin,
    monkeypatch,
):
    """Independent feature gating rejects binary messages before parsing."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(
        module,
        plugin,
        connection,
        server_features=(protocol.FEATURE_HA_EXPORT_NUMERIC_V1,),
    )
    assert plugin._protocol_selection.features == (
        protocol.FEATURE_HA_EXPORT_NUMERIC_V1,
    )
    parse_calls = []

    def record_parse(*args, **kwargs):
        parse_calls.append((args, kwargs))
        raise AssertionError

    monkeypatch.setattr(protocol, "parse_binary_apply", record_parse)
    binary_apply = protocol.sign_envelope(
        session_key,
        protocol_version=protocol.PROTOCOL_VERSION_V2,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=2,
        payload={"schema": 1, "type": "binary_apply"},
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(binary_apply)},
    )

    assert parse_calls == []
    assert domoticz.devices == {}
    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected


@pytest.mark.parametrize(
    ("selected_protocol", "wrong_version"),
    [
        (MISSING, 1),
        (None, 2),
    ],
)
def test_authenticated_envelope_from_wrong_protocol_version_is_rejected(
    loaded_plugin,
    selected_protocol,
    wrong_version,
):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(
        module,
        subprotocol=selected_protocol,
    )
    session_key, session_id = _complete_handshake(module, plugin, connection)
    wrong_envelope = protocol.sign_envelope(
        session_key,
        protocol_version=wrong_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=2,
        payload={"type": "ping", "id": protocol.generate_nonce()},
    )

    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(wrong_envelope)},
    )

    assert domoticz.devices == {}
    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected


def test_signed_create_uses_stable_custom_sensor_and_survives_lost_ack(
    loaded_plugin,
):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    action = _numeric_action(protocol)

    first = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="create-1",
        action=action,
    )
    second = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="create-1",
        action=action,
    )

    expected_id = "HAYNEMTCVS4EAV2SYARQERJIL"
    assert first.status is protocol.ApplyResultStatus.CONFIRMED
    assert first.target_id == expected_id
    assert first.source == action.capability.source
    assert second == first
    assert len(expected_id) == 25
    assert len(domoticz.create_calls) == 1
    unit = domoticz.devices[expected_id].Units[1]
    assert unit.Name == "Outdoor temperature"
    assert unit.Type == 243
    assert unit.SubType == 31
    assert unit.Options == {"Custom": "1;deg C"}
    assert unit.Used == 1
    assert unit.nValue == 0
    assert unit.sValue == "12.5"
    assert domoticz.devices[expected_id].TimedOut == 0
    assert len(unit.updates) == 1
    assert unit.refreshes == 2


def test_signed_create_uses_native_sensor_and_survives_lost_ack(
    loaded_plugin,
):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    action = _numeric_action(
        protocol,
        semantic="temperature",
        unit="°F",
        value=68,
    )

    first = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="native-create-1",
        action=action,
    )
    second = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="native-create-1",
        action=action,
    )

    expected_id = module._device_id_for_source(action.capability.source)
    assert first.status is protocol.ApplyResultStatus.CONFIRMED
    assert first.target_id == expected_id
    assert second == first
    assert len(domoticz.create_calls) == 1
    assert domoticz.create_calls[0] == {
        "Name": "Outdoor temperature",
        "DeviceID": expected_id,
        "Unit": 1,
        "Type": 80,
        "Subtype": 5,
        "Switchtype": 0,
        "Used": 1,
    }
    unit = domoticz.devices[expected_id].Units[1]
    assert unit.SwitchType == 0
    assert unit.Options == {}
    assert unit.nValue == 0
    assert unit.sValue == "20.0"


def test_signed_binary_create_is_idempotent_across_reconnects(loaded_plugin):
    """A repeated passive create adopts the deterministic existing switch."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    first_plugin, first_connection = _start_and_upgrade(module)
    first_key, first_session_id = _complete_handshake(
        module,
        first_plugin,
        first_connection,
    )
    action = _binary_action(protocol, semantic="door")

    first = _send_binary_apply(
        module,
        first_plugin,
        first_connection,
        first_key,
        first_session_id,
        request_id="binary-create-1",
        action=action,
    )
    repeated = _send_binary_apply(
        module,
        first_plugin,
        first_connection,
        first_key,
        first_session_id,
        request_id="binary-create-repeated-1",
        action=action,
    )

    second_plugin, second_connection = _start_and_upgrade(module)
    second_key, second_session_id = _complete_handshake(
        module,
        second_plugin,
        second_connection,
    )
    reconnected = _send_binary_apply(
        module,
        second_plugin,
        second_connection,
        second_key,
        second_session_id,
        request_id="binary-create-reconnected-1",
        action=action,
    )

    expected_id = module._device_id_for_source(action.capability.source)
    assert first.status is protocol.ApplyResultStatus.CONFIRMED
    assert first.target_id == expected_id
    assert first.source == action.capability.source
    assert repeated.status is protocol.ApplyResultStatus.CONFIRMED
    assert reconnected.status is protocol.ApplyResultStatus.CONFIRMED
    assert len(domoticz.create_calls) == 1
    assert domoticz.create_calls[0] == {
        "Name": "Back door",
        "DeviceID": expected_id,
        "Unit": 1,
        "Type": 244,
        "Subtype": 73,
        "Switchtype": 11,
        "Used": 1,
    }
    unit = domoticz.devices[expected_id].Units[1]
    assert unit.nValue == 1
    assert unit.sValue == "On"
    assert domoticz.devices[expected_id].TimedOut == 0


def test_binary_update_and_unavailable_preserve_last_state(loaded_plugin):
    """Binary changes converge in place and unavailable retains the value."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    create_action = _binary_action(protocol, semantic="motion")
    created = _send_binary_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="binary-motion-create-1",
        action=create_action,
    )
    device_id = created.target_id
    create_count = len(domoticz.create_calls)

    updated = _send_binary_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="binary-motion-update-1",
        action=_binary_action(
            protocol,
            kind="update",
            target_id=device_id,
            value=False,
            name="Hall motion",
            semantic="motion",
        ),
    )
    unavailable_action = _binary_action(
        protocol,
        kind="mark_unavailable",
        target_id=device_id,
        value=None,
        availability="unavailable",
        stale=True,
        name="Hall motion",
        semantic="motion",
    )
    unavailable = _send_binary_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="binary-motion-unavailable-1",
        action=unavailable_action,
    )
    domoticz.devices[device_id].TimedOut = 0
    repeated_unavailable = _send_binary_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="binary-motion-unavailable-reconnect-1",
        action=unavailable_action,
    )

    unit = domoticz.devices[device_id].Units[1]
    assert updated.status is protocol.ApplyResultStatus.CONFIRMED
    assert unavailable.status is protocol.ApplyResultStatus.CONFIRMED
    assert repeated_unavailable.status is protocol.ApplyResultStatus.CONFIRMED
    assert len(domoticz.create_calls) == create_count
    assert unit.Name == "Hall motion"
    assert unit.Type == 244
    assert unit.SubType == 73
    assert unit.SwitchType == 8
    assert unit.nValue == 0
    assert unit.sValue == "Off"
    assert domoticz.devices[device_id].TimedOut == 1


def test_binary_apply_rejects_incompatible_collision(loaded_plugin):
    """A deterministic ID collision never retypes an unrelated device."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    action = _binary_action(protocol, semantic="smoke")
    device_id = module._device_id_for_source(action.capability.source)
    module.Domoticz.Unit(
        Name="Do not replace",
        DeviceID=device_id,
        Unit=1,
        Type=244,
        Subtype=73,
        Switchtype=0,
    ).Create()
    incompatible = domoticz.devices[device_id].Units[1]

    result = _send_binary_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="binary-collision-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert result.target_id is None
    assert result.source is None
    assert domoticz.devices[device_id].Units[1] is incompatible
    assert incompatible.Name == "Do not replace"
    assert incompatible.SwitchType == 0
    assert not incompatible.deleted


def test_binary_command_callback_is_an_explicit_read_only_noop(loaded_plugin):
    """A Domoticz control never changes state or sends a reverse command."""
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    action = _binary_action(protocol, value=True)
    created = _send_binary_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="binary-read-only-1",
        action=action,
    )
    unit = domoticz.devices[created.target_id].Units[1]
    sent_before = list(connection.sent)
    module._plugin = plugin

    module.onCommand(created.target_id, 1, "Off", 0, None)

    assert unit.nValue == 1
    assert unit.sValue == "On"
    assert connection.sent == sent_before
    assert domoticz.statuses[-1] == "Home Assistant export devices are read-only."
    assert not hasattr(module, "onDeviceModified")


def test_update_adopts_and_converges_matching_native_sensor(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    source_action = _numeric_action(
        protocol,
        semantic="temperature",
        unit="°C",
    )
    device_id = module._device_id_for_source(source_action.capability.source)
    module.Domoticz.Unit(
        Name="Old name",
        DeviceID=device_id,
        Unit=1,
        Type=80,
        Subtype=5,
        Switchtype=0,
        Options={"Native": "leave-me-alone"},
    ).Create()
    existing = domoticz.devices[device_id].Units[1]
    existing.Used = 0
    existing.nValue = 7
    existing.sValue = "-1"
    domoticz.devices[device_id].TimedOut = 1
    created_before = len(domoticz.create_calls)
    action = _numeric_action(
        protocol,
        kind="update",
        target_id=device_id,
        semantic="temperature",
        unit="K",
        value=293.15,
        name="Conservatory temperature",
        state_class="measurement",
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="native-update-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert len(domoticz.create_calls) == created_before
    assert domoticz.devices[device_id].Units[1] is existing
    assert existing.Name == "Conservatory temperature"
    assert existing.Options == {"Native": "leave-me-alone"}
    assert existing.Used == 1
    assert existing.nValue == 0
    assert existing.sValue == "20.0"
    assert domoticz.devices[device_id].TimedOut == 0
    assert existing.updates[-1] == {
        "Log": False,
        "UpdateProperties": True,
    }


def test_unavailable_native_sensor_retains_values_and_options(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    source_action = _numeric_action(
        protocol,
        semantic="humidity",
        unit="%",
    )
    device_id = module._device_id_for_source(source_action.capability.source)
    module.Domoticz.Unit(
        Name="Humidity",
        DeviceID=device_id,
        Unit=1,
        Type=81,
        Subtype=1,
        Switchtype=0,
        Options={"Calibration": "7"},
        Used=1,
    ).Create()
    unit = domoticz.devices[device_id].Units[1]
    unit.nValue = 48
    unit.sValue = "1"
    domoticz.devices[device_id].TimedOut = 0
    action = _numeric_action(
        protocol,
        kind="mark_unavailable",
        target_id=device_id,
        semantic="humidity",
        unit="%",
        value=None,
        availability="unavailable",
        stale=True,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="native-unavailable-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert unit.nValue == 48
    assert unit.sValue == "1"
    assert unit.Options == {"Calibration": "7"}
    assert domoticz.devices[device_id].TimedOut == 1


def test_profile_change_collision_is_rejected_without_retyping(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    custom_action = _numeric_action(protocol, unit="°C")
    created = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="profile-custom-1",
        action=custom_action,
    )
    device_id = created.target_id
    unit = domoticz.devices[device_id].Units[1]
    create_count = len(domoticz.create_calls)

    changed = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="profile-native-1",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id=device_id,
            semantic="temperature",
            unit="°C",
            value=20,
        ),
    )

    assert changed.status is protocol.ApplyResultStatus.REJECTED
    assert len(domoticz.create_calls) == create_count
    assert domoticz.devices[device_id].Units[1] is unit
    assert unit.Type == 243
    assert unit.SubType == 31
    assert unit.sValue == "12.5"
    assert not unit.deleted


def test_native_profile_requires_exact_switch_type(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    action = _numeric_action(
        protocol,
        semantic="temperature",
        unit="°C",
    )
    device_id = module._device_id_for_source(action.capability.source)
    module.Domoticz.Unit(
        Name="Do not replace",
        DeviceID=device_id,
        Unit=1,
        Type=80,
        Subtype=5,
        Switchtype=1,
    ).Create()
    incompatible = domoticz.devices[device_id].Units[1]

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="native-switch-collision-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert domoticz.devices[device_id].Units[1] is incompatible
    assert incompatible.Name == "Do not replace"
    assert not incompatible.deleted


@pytest.mark.parametrize(
    ("semantic", "unit", "value"),
    [
        ("humidity", "%", -0.1),
        ("humidity", "%", 100.1),
        ("sound_pressure", "dB", -0.1),
        ("carbon_dioxide", "ppm", 1_000_000.1),
        (None, "UV index", 30.1),
    ],
)
def test_invalid_native_value_is_rejected_before_device_creation(
    loaded_plugin,
    semantic,
    unit,
    value,
):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="invalid-native-1",
        action=_numeric_action(
            protocol,
            semantic=semantic,
            unit=unit,
            value=value,
        ),
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert domoticz.create_calls == []
    assert domoticz.devices == {}


def test_create_adopts_matching_existing_custom_sensor(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    action = _numeric_action(protocol)
    device_id = module._device_id_for_source(action.capability.source)
    module.Domoticz.Unit(
        Name="Old name",
        DeviceID=device_id,
        Unit=1,
        Type=243,
        Subtype=31,
        Options={"Custom": "1;old"},
    ).Create()
    existing = domoticz.devices[device_id].Units[1]
    existing.nValue = 9
    existing.sValue = "99"
    domoticz.devices[device_id].TimedOut = 1
    created_before = len(domoticz.create_calls)

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="adopt-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert result.target_id == device_id
    assert len(domoticz.create_calls) == created_before
    assert domoticz.devices[device_id].Units[1] is existing
    assert existing.Name == action.capability.name
    assert existing.Options == {"Custom": "1;deg C"}
    assert existing.Used == 1
    assert existing.nValue == 0
    assert existing.sValue == "12.5"
    assert domoticz.devices[device_id].TimedOut == 0


def test_update_converges_complete_numeric_state(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    source_action = _numeric_action(protocol)
    device_id = module._device_id_for_source(source_action.capability.source)
    module.Domoticz.Unit(
        Name="Old name",
        DeviceID=device_id,
        Unit=1,
        Type=243,
        Subtype=31,
        Options={"Custom": "1;old"},
    ).Create()
    created_before = len(domoticz.create_calls)
    action = _numeric_action(
        protocol,
        kind="update",
        target_id=device_id,
        value=18,
        name="Garden temperature",
        unit="C",
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="update-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert len(domoticz.create_calls) == created_before
    unit = domoticz.devices[device_id].Units[1]
    assert unit.Name == "Garden temperature"
    assert unit.Options == {"Custom": "1;C"}
    assert unit.Used == 1
    assert unit.nValue == 0
    assert unit.sValue == "18"
    assert domoticz.devices[device_id].TimedOut == 0
    assert unit.updates[-1] == {
        "Log": False,
        "UpdateOptions": True,
        "UpdateProperties": True,
    }


def test_available_update_recreates_a_missing_expected_sensor(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    source_action = _numeric_action(protocol)
    device_id = module._device_id_for_source(source_action.capability.source)
    action = _numeric_action(
        protocol,
        kind="update",
        target_id=device_id,
        value=19.25,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="repair-update-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert result.target_id == device_id
    assert len(domoticz.create_calls) == 1
    unit = domoticz.devices[device_id].Units[1]
    assert unit.Type == 243
    assert unit.SubType == 31
    assert unit.Used == 1
    assert unit.sValue == "19.25"


def test_unavailable_retains_value_and_sets_timed_out(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    source_action = _numeric_action(protocol)
    device_id = module._device_id_for_source(source_action.capability.source)
    module.Domoticz.Unit(
        Name="Outdoor temperature",
        DeviceID=device_id,
        Unit=1,
        Type=243,
        Subtype=31,
        Options={"Custom": "1;deg C"},
    ).Create()
    unit = domoticz.devices[device_id].Units[1]
    unit.nValue = 0
    unit.sValue = "21.75"
    domoticz.devices[device_id].TimedOut = 0
    action = _numeric_action(
        protocol,
        kind="mark_unavailable",
        target_id=device_id,
        value=None,
        availability="unavailable",
        stale=True,
    )

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="unavailable-1",
        action=action,
    )

    assert result.status is protocol.ApplyResultStatus.CONFIRMED
    assert unit.nValue == 0
    assert unit.sValue == "21.75"
    assert domoticz.devices[device_id].TimedOut == 1
    assert not unit.deleted


def test_unsupported_action_is_rejected_locally_and_malformed_action_closes(
    loaded_plugin,
):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    numeric = _numeric_action(protocol)
    binary = protocol.ReconciliationAction(
        kind=protocol.ReconciliationActionKind.CREATE,
        capability=protocol.Capability(
            source=numeric.capability.source,
            kind=protocol.CapabilityKind.BINARY,
            name="Door",
            value=True,
        ),
    )

    with pytest.raises(module.DomoticzApplyError):
        plugin._apply_action(binary)

    malformed_payload = protocol.build_apply(
        plugin._protocol_selection,
        "malformed-1",
        numeric,
    )
    malformed_payload["action"]["kind"] = "delete"
    malformed_envelope = protocol.sign_envelope(
        session_key,
        protocol_version=plugin._protocol_version,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=plugin._in_sequence + 1,
        payload=malformed_payload,
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(malformed_envelope)},
    )

    assert domoticz.devices == {}
    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert connection.sent[-1]["Operation"] == "Close"
    logs = "\n".join(domoticz.logs + domoticz.errors)
    assert module.Parameters["Mode3"] not in logs
    assert "delete" not in logs


def test_apply_rejects_wrong_target_or_incompatible_existing_device(
    loaded_plugin,
):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    create = _numeric_action(protocol)
    device_id = module._device_id_for_source(create.capability.source)
    module.Domoticz.Unit(
        Name="Do not replace",
        DeviceID=device_id,
        Unit=1,
        Type=244,
        Subtype=73,
    ).Create()
    incompatible = domoticz.devices[device_id].Units[1]

    collision = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="collision-1",
        action=create,
    )
    wrong_target = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="wrong-target-1",
        action=_numeric_action(
            protocol,
            kind="update",
            target_id="HAWRONGTARGET",
            value=19,
        ),
    )

    assert collision.status is protocol.ApplyResultStatus.REJECTED
    assert wrong_target.status is protocol.ApplyResultStatus.REJECTED
    assert domoticz.devices[device_id].Units[1] is incompatible
    assert incompatible.Name == "Do not replace"
    assert not incompatible.deleted


def test_apply_confirms_only_after_refresh_and_reread(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)
    domoticz.corrupt_refreshes = True

    result = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="not-confirmed-1",
        action=_numeric_action(protocol),
    )

    assert result.status is protocol.ApplyResultStatus.REJECTED
    assert result.target_id is None
    assert result.source is None


@pytest.mark.parametrize("continuation_type", [bytes, bytearray])
def test_legacy_mixed_fragmentation_survives_interleaved_ping(
    loaded_plugin, continuation_type
):
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module, subprotocol=None)
    hello = protocol.parse_hello(
        protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    )
    context = protocol.make_handshake_context(hello, protocol.generate_nonce())
    challenge_text = protocol.canonical_json_dumps(
        protocol.build_challenge(module.Parameters["Mode3"], context)
    )
    midpoint = len(challenge_text) // 2

    plugin.onMessage(
        connection,
        {"Payload": challenge_text[:midpoint], "Finish": False},
    )
    control_payload = bytearray(b"legacy-ping")
    plugin.onMessage(
        connection,
        {"Operation": "Ping", "Payload": control_payload},
    )
    assert connection.sent[-1]["Operation"] == "Pong"
    assert connection.sent[-1]["Payload"] == control_payload

    plugin.onMessage(
        connection,
        {
            "Payload": continuation_type(challenge_text[midpoint:].encode("utf-8")),
            "Finish": True,
        },
    )

    authenticate = protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    protocol.verify_authenticate(module.Parameters["Mode3"], context, authenticate)
    assert plugin.phase == module.PHASE_WAIT_PROTOCOL_READY
    assert plugin._fragment_parts == []


def test_each_protocol_phase_gets_its_own_timeout_window(loaded_plugin):
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module, subprotocol=None)
    hello = protocol.parse_hello(
        protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    )
    context = protocol.make_handshake_context(hello, protocol.generate_nonce())

    plugin.onHeartbeat()
    assert plugin.phase == module.PHASE_WAIT_CHALLENGE
    plugin.onMessage(
        connection,
        {
            "Payload": protocol.canonical_json_dumps(
                protocol.build_challenge(module.Parameters["Mode3"], context)
            )
        },
    )
    assert plugin.phase == module.PHASE_WAIT_PROTOCOL_READY
    assert plugin._phase_started_tick == 1

    plugin.onHeartbeat()
    assert plugin.phase == module.PHASE_WAIT_PROTOCOL_READY
    session_key = protocol.derive_session_key(module.Parameters["Mode3"], context)
    plugin.onMessage(
        connection,
        {
            "Payload": protocol.canonical_json_dumps(
                protocol.build_ready(session_key, context)
            )
        },
    )
    assert plugin.phase == module.PHASE_WAIT_APPLICATION_READY
    assert plugin._phase_started_tick == 2

    plugin.onHeartbeat()
    assert plugin.phase == module.PHASE_WAIT_APPLICATION_READY
    application_ready = protocol.sign_envelope(
        session_key,
        protocol_version=protocol.PROTOCOL_VERSION,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=protocol.derive_session_id(session_key, context),
        sequence=1,
        payload={"type": "ready"},
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(application_ready)},
    )
    assert plugin.phase == module.PHASE_READY


def test_invalid_proof_is_rejected_without_logging_sensitive_data(loaded_plugin):
    module, domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    hello = protocol.parse_v2_hello(
        protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    )
    context = protocol.make_v2_handshake_context(
        hello,
        protocol.generate_nonce(),
        server_protocols=protocol.SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
        server_features=protocol.SUPPORTED_V2_FEATURES,
    )
    challenge = protocol.build_v2_challenge(module.Parameters["Mode3"], context)
    challenge["server_proof"] = protocol.generate_pairing_key()

    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(challenge)},
    )

    all_logs = "\n".join(domoticz.logs + domoticz.errors)
    assert plugin.phase == module.PHASE_DISCONNECTED
    assert module.Parameters["Mode3"] not in all_logs
    assert challenge["server_proof"] not in all_logs
    assert "server_proof" not in all_logs


def test_reconnect_backoff_is_heartbeat_driven(loaded_plugin):
    module, domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    first = plugin.connection

    plugin.onConnect(first, 1, "ignored")
    assert len(domoticz.connections) == 1
    plugin.onHeartbeat()
    assert len(domoticz.connections) == 2
    second = plugin.connection

    plugin.onConnect(second, 1, "ignored")
    plugin.onHeartbeat()
    assert len(domoticz.connections) == 2
    plugin.onHeartbeat()
    assert len(domoticz.connections) == 3


def test_connecting_timeout_does_not_need_a_domoticz_callback(loaded_plugin):
    module, domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection

    for _ in range(module._CONNECT_TIMEOUT_TICKS):
        plugin.onHeartbeat()

    assert plugin.phase == module.PHASE_DISCONNECTED
    assert connection.disconnected
    assert connection.sent == []
    assert len(domoticz.connections) == 1

    plugin.onHeartbeat()
    assert len(domoticz.connections) == 2


@pytest.mark.parametrize("probe_name", ["Connected", "Connecting"])
def test_probe_failure_closes_transport_before_replacement(
    loaded_plugin,
    monkeypatch,
    probe_name,
):
    module, domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    first = plugin.connection
    original_connection_factory = domoticz.Connection
    disconnected_before_replacement = []
    failure_detail = f"sensitive {probe_name} failure detail"

    def raise_probe_failure():
        raise RuntimeError(failure_detail)

    def create_replacement(**kwargs):
        disconnected_before_replacement.append(first.disconnected)
        return original_connection_factory(**kwargs)

    monkeypatch.setattr(first, probe_name, raise_probe_failure)
    monkeypatch.setattr(domoticz, "Connection", create_replacement)

    plugin._connect()

    assert first.disconnected
    assert disconnected_before_replacement == [True]
    assert plugin.connection is domoticz.connections[-1]
    assert plugin.connection is not first
    assert plugin.connection.connecting
    assert len(domoticz.connections) == 2
    assert domoticz.errors == [
        "Could not inspect the existing Home Assistant sync connection."
    ]
    assert failure_detail not in "\n".join(domoticz.errors)


def test_connect_failure_closes_new_transport_before_scheduling_reconnect(
    loaded_plugin,
    monkeypatch,
):
    module, domoticz = loaded_plugin
    original_connection_factory = domoticz.Connection
    failure_detail = "sensitive Connect failure detail"

    def create_failing_connection(**kwargs):
        connection = original_connection_factory(**kwargs)

        def raise_connect_failure():
            raise RuntimeError(failure_detail)

        monkeypatch.setattr(connection, "Connect", raise_connect_failure)
        return connection

    monkeypatch.setattr(domoticz, "Connection", create_failing_connection)
    plugin = module.DomoticzSyncPlugin()

    plugin.onStart()

    connection = domoticz.connections[0]
    assert connection.disconnected
    assert plugin.connection is None
    assert plugin.phase == module.PHASE_DISCONNECTED
    assert domoticz.errors == ["Could not connect to Home Assistant."]
    assert failure_detail not in "\n".join(domoticz.errors)


def test_cleanup_failures_are_reported_without_exception_details(
    loaded_plugin,
    monkeypatch,
):
    module, domoticz = loaded_plugin
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection
    calls = []
    send_failure_detail = "sensitive close-send failure detail"
    disconnect_failure_detail = "sensitive disconnect failure detail"

    def raise_send_failure(document):
        calls.append(("send", document["Operation"]))
        raise RuntimeError(send_failure_detail)

    def raise_disconnect_failure():
        calls.append(("disconnect",))
        raise RuntimeError(disconnect_failure_detail)

    monkeypatch.setattr(connection, "Send", raise_send_failure)
    monkeypatch.setattr(connection, "Disconnect", raise_disconnect_failure)

    plugin.onStop()

    assert calls == [("send", "Close"), ("disconnect",)]
    assert plugin.connection is None
    assert plugin.phase == module.PHASE_STOPPED
    assert domoticz.errors == [
        "A Home Assistant sync connection cleanup operation failed."
    ]
    all_errors = "\n".join(domoticz.errors)
    assert send_failure_detail not in all_errors
    assert disconnect_failure_detail not in all_errors


def test_global_callbacks_do_not_forward_procedure_return_values(
    loaded_plugin,
    monkeypatch,
):
    module, _domoticz = loaded_plugin
    marker = object()
    direct_calls = []

    def returning_handler(*args):
        direct_calls.append(args)
        return marker

    assert module._callback("test", returning_handler, 1, 2) is None
    assert direct_calls == [(1, 2)]

    wrapper_calls = []

    def returning_callback(*args):
        wrapper_calls.append(args)
        return marker

    monkeypatch.setattr(module, "_callback", returning_callback)
    connection = object()
    data = object()

    results = [
        module.onStart(),
        module.onStop(),
        module.onConnect(connection, 0, "connected"),
        module.onMessage(connection, data),
        module.onDisconnect(connection),
        module.onHeartbeat(),
        module.onCommand("device", 1, "Off", 0, None),
    ]

    assert results == [None] * 7
    assert [call[0] for call in wrapper_calls] == [
        "start",
        "stop",
        "connect",
        "message",
        "disconnect",
        "heartbeat",
        "command",
    ]
