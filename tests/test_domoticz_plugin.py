"""Tests for the root Domoticz companion plugin."""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
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
        self.Name = kwargs.get("Name")
        self.DeviceID = kwargs.get("DeviceID")
        self.Unit = kwargs.get("Unit", 1)
        self.Type = kwargs.get("Type", 0)
        self.SubType = kwargs.get("Subtype", kwargs.get("SubType", 0))
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
        device = self._domoticz.devices.get(self.DeviceID)
        if device is not None:
            device.Units.pop(self.Unit, None)


class FakeDevice:
    """Container matching DomoticzEx's extended device model."""

    def __init__(self, domoticz):
        self._domoticz = domoticz
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
                unit.DeviceID,
                FakeDevice(self._domoticz),
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


def _upgrade_response(connection, *, extensions=None, valid_accept=True):
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
    return {"Status": "101", "Headers": headers}


def _start_and_upgrade(module):
    plugin = module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = plugin.connection
    connection.connecting = False
    connection.connected = True
    plugin.onConnect(connection, 0, "ignored")
    plugin.onMessage(connection, _upgrade_response(connection))
    return plugin, connection


def _complete_handshake(module, plugin, connection):
    protocol = module.wire_protocol
    hello_document = protocol.canonical_json_loads(connection.sent[-1]["Payload"])
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
    protocol.verify_authenticate(module.Parameters["Mode3"], context, authenticate)
    session_key = protocol.derive_session_key(module.Parameters["Mode3"], context)
    plugin.onMessage(
        connection,
        {
            "Payload": protocol.canonical_json_dumps(
                protocol.build_ready(session_key, context)
            )
        },
    )
    session_id = protocol.derive_session_id(session_key, context)
    inventory = protocol.verify_envelope(
        session_key,
        protocol.canonical_json_loads(connection.sent[-1]["Payload"]),
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=0,
    )
    assert inventory.payload == {"type": "inventory", "targets": []}

    application_ready = protocol.sign_envelope(
        session_key,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=1,
        payload={"type": "ready"},
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
        unit=unit,
    )
    return protocol.ReconciliationAction(
        kind=protocol.ReconciliationActionKind(kind),
        capability=capability,
        target_id=target_id,
        stale=stale,
    )


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
        payload = protocol.build_apply(request_id, action)
    previous_out_sequence = plugin._out_sequence
    envelope = protocol.sign_envelope(
        session_key,
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
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=previous_out_sequence,
    )
    return protocol.parse_apply_result(response.payload)


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
    release_config = json.loads(
        (ROOT / "release-please-config.json").read_text(encoding="utf-8")
    )
    extra_files = release_config["packages"]["."]["extra-files"]

    assert "# x-release-please-start-version" in source
    assert "# x-release-please-end" in source
    assert {"type": "generic", "path": "plugin.py"} in extra_files
    assert readme.count("x-release-please-start-version") == 2
    assert readme.count("x-release-please-end") == 2
    assert {"type": "generic", "path": "README.md"} in extra_files


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


def test_mutual_handshake_inventory_and_signed_ping_pong(loaded_plugin):
    module, _domoticz = loaded_plugin
    protocol = module.wire_protocol
    plugin, connection = _start_and_upgrade(module)
    session_key, session_id = _complete_handshake(module, plugin, connection)

    server_ping_id = protocol.generate_nonce()
    inbound_ping = protocol.sign_envelope(
        session_key,
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
        expected_direction=protocol.DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=2,
    )
    assert outbound_ping.payload["type"] == "ping"
    protocol.validate_nonce(outbound_ping.payload["id"])

    inbound_pong = protocol.sign_envelope(
        session_key,
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


def test_unsupported_action_is_rejected_and_malformed_action_closes(
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

    unsupported = _send_apply(
        module,
        plugin,
        connection,
        session_key,
        session_id,
        request_id="unsupported-1",
        action=binary,
    )
    malformed_payload = protocol.build_apply("malformed-1", numeric)
    malformed_payload["action"]["kind"] = "delete"
    malformed_envelope = protocol.sign_envelope(
        session_key,
        direction=protocol.DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=plugin._in_sequence + 1,
        payload=malformed_payload,
    )
    plugin.onMessage(
        connection,
        {"Payload": protocol.canonical_json_dumps(malformed_envelope)},
    )

    assert unsupported.status is protocol.ApplyResultStatus.REJECTED
    assert unsupported.target_id is None
    assert unsupported.source is None
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
    plugin, connection = _start_and_upgrade(module)
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
    plugin, connection = _start_and_upgrade(module)
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
    hello = protocol.parse_hello(
        protocol.canonical_json_loads(connection.sent[-1]["Payload"])
    )
    context = protocol.make_handshake_context(hello, protocol.generate_nonce())
    challenge = protocol.build_challenge(module.Parameters["Mode3"], context)
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
