# x-release-please-start-version
"""
<plugin
    key="HADomoticzSync"
    name="Home Assistant Domoticz Sync"
    author="Vincent van Adrighem"
    version="0.3.0"
    wikilink="https://github.com/adrighem/ha-domoticz-sync">
    <description>
        <h2>Home Assistant Domoticz Sync</h2>
        Synchronizes selected Home Assistant entities to Domoticz over an
        authenticated connection.
    </description>
    <params>
        <param
            field="Address"
            label="Home Assistant host"
            width="250px"
            required="true"/>
        <param
            field="Port"
            label="Home Assistant port"
            width="75px"
            required="true"
            default="8123"/>
        <param field="Mode1" label="Connection" width="200px" required="true">
            <options>
                <option label="WebSocket (WS)" value="WS" default="true"/>
                <option label="Secure WebSocket (WSS)" value="WSS"/>
            </options>
        </param>
        <param field="Mode2" label="Link ID" width="350px" required="true"/>
        <param
            field="Mode3"
            label="Pairing key"
            width="350px"
            required="true"
            password="true"/>
    </params>
</plugin>
"""
# x-release-please-end

import base64
import hashlib
import hmac
import importlib.util
import math
import os
import secrets
import sys
import uuid
from typing import Callable, NamedTuple

import DomoticzEx as Domoticz

_ENDPOINT = "/api/domoticz_sync/websocket"
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CONFIG_DESTINATION_ID = "domoticz_sync_destination_id"
_MAX_MESSAGE_BYTES = 64 * 1024
_HEARTBEAT_SECONDS = 5
_CONNECT_TIMEOUT_TICKS = 3
_HANDSHAKE_TIMEOUT_TICKS = 2
_PING_INTERVAL_TICKS = 6
_PING_TIMEOUT_TICKS = 3
_MAX_RECONNECT_TICKS = 32
_CUSTOM_SENSOR_TYPE = 243
_CUSTOM_SENSOR_SUBTYPE = 31
_TARGET_UNIT = 1
_DEFAULT_SWITCH_TYPE = 0

PHASE_STOPPED = "stopped"
PHASE_DISCONNECTED = "disconnected"
PHASE_CONNECTING = "connecting"
PHASE_UPGRADING = "upgrading"
PHASE_WAIT_CHALLENGE = "wait_challenge"
PHASE_WAIT_PROTOCOL_READY = "wait_protocol_ready"
PHASE_WAIT_APPLICATION_READY = "wait_application_ready"
PHASE_READY = "ready"


def _load_wire_protocol():
    """Load the vendored neutral core without importing Home Assistant."""
    candidates = []
    plugin_file = globals().get("__file__")
    if isinstance(plugin_file, str):
        candidates.append(os.path.dirname(os.path.realpath(plugin_file)))

    parameters = globals().get("Parameters")
    if isinstance(parameters, dict):
        home_folder = parameters.get("HomeFolder")
        if isinstance(home_folder, str) and home_folder:
            candidates.append(os.path.realpath(home_folder))

    for base in candidates:
        package_root = os.path.join(base, "custom_components", "domoticz_sync")
        protocol_file = os.path.join(package_root, "core", "protocol.py")
        if os.path.isfile(protocol_file):
            protocol_file = os.path.realpath(protocol_file)
            module_suffix = hashlib.sha256(protocol_file.encode("utf-8")).hexdigest()[
                :16
            ]
            module_name = "_ha_domoticz_sync_wire_protocol_" + module_suffix
            existing = sys.modules.get(module_name)
            if existing is not None:
                loaded_file = getattr(existing, "__file__", None)
                if (
                    isinstance(loaded_file, str)
                    and os.path.realpath(loaded_file) == protocol_file
                ):
                    return existing
                raise RuntimeError(
                    "The bundled Domoticz sync core could not be loaded."
                )

            specification = importlib.util.spec_from_file_location(
                module_name,
                protocol_file,
                submodule_search_locations=[os.path.dirname(protocol_file)],
            )
            if specification is None or specification.loader is None:
                raise RuntimeError(
                    "The bundled Domoticz sync core could not be loaded."
                )
            protocol = importlib.util.module_from_spec(specification)
            sys.modules[module_name] = protocol
            try:
                specification.loader.exec_module(protocol)
            except Exception:
                sys.modules.pop(module_name, None)
                raise RuntimeError(
                    "The bundled Domoticz sync core could not be loaded."
                ) from None
            return protocol

    raise RuntimeError("The bundled Domoticz sync core could not be loaded.")


wire_protocol = _load_wire_protocol()


class PluginConfigurationError(Exception):
    """Raised for unusable local plugin configuration."""


class DomoticzApplyError(Exception):
    """Raised when a requested target state cannot be confirmed."""


def _canonical_destination_id(value):
    if not isinstance(value, str):
        raise PluginConfigurationError
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise PluginConfigurationError from None
    if str(parsed) != value:
        raise PluginConfigurationError
    wire_protocol.validate_destination_id(value)
    return value


def _header_value(headers, name):
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name.lower():
            if isinstance(value, str):
                return value
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return ",".join(value)
            return None
    return None


def _header_count(headers, name):
    if not isinstance(headers, dict):
        return 0
    return sum(
        1 for key in headers if isinstance(key, str) and key.lower() == name.lower()
    )


def _source_document(source):
    """Return the exact canonical identity document used for target IDs."""
    return {
        "system": source.system,
        "instance_id": source.instance_id,
        "object_id": source.object_id,
        "capability_id": source.capability_id,
    }


def _device_id_for_source(source):
    """Derive one stable Domoticz DeviceID from a complete source identity."""
    identity = wire_protocol.canonical_json_bytes(_source_document(source))
    digest = hashlib.sha256(identity).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")
    return "HA" + encoded[:23]


def _numeric_s_value(value):
    """Format one finite protocol number without lossy rounding."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DomoticzApplyError
    return str(value)


def _custom_sensor_options(unit):
    """Build Domoticz's Custom Sensor options for an optional source unit."""
    return {"Custom": "1;" + (unit if unit is not None else "")}


class _TargetProfile(NamedTuple):
    """Immutable Domoticz unit shape and value codec."""

    type_id: int
    subtype: int
    switch_type: int
    encoder: Callable[[object, object], tuple[int, str]]
    manages_options: bool


def _finite_number(value):
    """Return one finite protocol number, rejecting bool explicitly."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DomoticzApplyError
    return value


def _scaled_value(value, unit, factors):
    """Convert a finite value using one explicit source-unit factor."""
    value = _finite_number(value)
    try:
        converted = value * factors[unit]
    except (KeyError, TypeError):
        raise DomoticzApplyError from None
    if not math.isfinite(converted):
        raise DomoticzApplyError
    return converted


def _encode_custom(value, _unit):
    return 0, _numeric_s_value(value)


def _encode_temperature(value, unit):
    value = _finite_number(value)
    if unit in {"°C", "celsius"}:
        converted = value
    elif unit in {"°F", "fahrenheit"}:
        converted = (value - 32) * 5 / 9
    elif unit in {"K", "kelvin"}:
        converted = value - 273.15
    else:
        raise DomoticzApplyError
    return 0, _numeric_s_value(converted)


def _round_positive_half_up(value, maximum):
    value = _finite_number(value)
    if value < 0 or value > maximum:
        raise DomoticzApplyError
    return int(math.floor(value + 0.5))


def _encode_humidity(value, unit):
    if unit not in {"%", "percent"}:
        raise DomoticzApplyError
    rounded = _round_positive_half_up(value, 100)
    if rounded < 25:
        status = "2"
    elif rounded <= 60:
        status = "1"
    else:
        status = "3"
    return rounded, status


def _encode_percentage(value, unit):
    if unit not in {"%", "percent"}:
        raise DomoticzApplyError
    return 0, _numeric_s_value(value)


# Keep conversion factors local and explicit. Lowercase word aliases are the
# stable neutral units already emitted by the Home Assistant source adapter.
_HPA_FACTORS = {
    "mPa": 0.00001,
    "Pa": 0.01,
    "hPa": 1,
    "hpa": 1,
    "kPa": 10,
    "bar": 1000,
    "cbar": 10,
    "mbar": 1,
    "mmHg": 1.333223684,
    "inHg": 33.863886667,
    "inH₂O": 2.4908891,
    "psi": 68.947572932,
}
_BAR_FACTORS = {unit: factor / 1000 for unit, factor in _HPA_FACTORS.items()}
_VOLT_FACTORS = {
    "μV": 0.000001,
    "µV": 0.000001,
    "mV": 0.001,
    "V": 1,
    "volt": 1,
    "kV": 1000,
    "MV": 1000000,
}
_AMPERE_FACTORS = {
    "μA": 0.000001,
    "µA": 0.000001,
    "mA": 0.001,
    "A": 1,
}
_WATT_FACTORS = {
    "mW": 0.001,
    "W": 1,
    "watt": 1,
    "kW": 1000,
    "MW": 1000000,
    "GW": 1000000000,
    "TW": 1000000000000,
    "BTU/h": 0.29307107,
}
_CENTIMETER_FACTORS = {
    "mm": 0.1,
    "cm": 1,
    "m": 100,
    "km": 100000,
    "in": 2.54,
    "ft": 30.48,
    "yd": 91.44,
    "mi": 160934.4,
    "nmi": 185200,
}
_KILOGRAM_FACTORS = {
    "μg": 0.000000001,
    "µg": 0.000000001,
    "mg": 0.000001,
    "g": 0.001,
    "kg": 1,
    "oz": 0.028349523125,
    "lb": 0.45359237,
    "st": 6.35029318,
}
_IRRADIANCE_FACTORS = {
    "W/m²": 1,
    "W/m2": 1,
    "BTU/(h⋅ft²)": 3.154590745,
}


def _encode_atmospheric_pressure(value, unit):
    converted = _scaled_value(value, unit, _HPA_FACTORS)
    return 0, _numeric_s_value(converted) + ";5"


def _encode_pressure(value, unit):
    return 0, _numeric_s_value(_scaled_value(value, unit, _BAR_FACTORS))


def _encode_voltage(value, unit):
    return 0, _numeric_s_value(_scaled_value(value, unit, _VOLT_FACTORS))


def _encode_current(value, unit):
    return 0, _numeric_s_value(_scaled_value(value, unit, _AMPERE_FACTORS))


def _encode_power(value, unit):
    return 0, _numeric_s_value(_scaled_value(value, unit, _WATT_FACTORS))


def _encode_illuminance(value, unit):
    if unit not in {"lx", "lux"}:
        raise DomoticzApplyError
    return 0, _numeric_s_value(value)


def _encode_distance(value, unit):
    return 0, _numeric_s_value(_scaled_value(value, unit, _CENTIMETER_FACTORS))


def _encode_weight(value, unit):
    return 0, _numeric_s_value(_scaled_value(value, unit, _KILOGRAM_FACTORS))


def _encode_sound_pressure(value, unit):
    if unit not in {"dB", "dBA"}:
        raise DomoticzApplyError
    return 0, str(_round_positive_half_up(value, 2147483647))


def _encode_irradiance(value, unit):
    converted = _scaled_value(value, unit, _IRRADIANCE_FACTORS)
    return 0, _numeric_s_value(converted)


def _encode_carbon_dioxide(value, unit):
    if unit != "ppm":
        raise DomoticzApplyError
    return _round_positive_half_up(value, 1000000), ""


_CUSTOM_PROFILE = _TargetProfile(
    _CUSTOM_SENSOR_TYPE,
    _CUSTOM_SENSOR_SUBTYPE,
    _DEFAULT_SWITCH_TYPE,
    _encode_custom,
    True,
)
_TEMPERATURE_PROFILE = _TargetProfile(80, 5, 0, _encode_temperature, False)
_HUMIDITY_PROFILE = _TargetProfile(81, 1, 0, _encode_humidity, False)
_PERCENTAGE_PROFILE = _TargetProfile(243, 6, 0, _encode_percentage, False)
_ATMOSPHERIC_PRESSURE_PROFILE = _TargetProfile(
    243, 26, 0, _encode_atmospheric_pressure, False
)
_PRESSURE_PROFILE = _TargetProfile(243, 9, 0, _encode_pressure, False)
_VOLTAGE_PROFILE = _TargetProfile(243, 8, 0, _encode_voltage, False)
_CURRENT_PROFILE = _TargetProfile(243, 23, 0, _encode_current, False)
_POWER_PROFILE = _TargetProfile(248, 1, 0, _encode_power, False)
_ILLUMINANCE_PROFILE = _TargetProfile(246, 1, 0, _encode_illuminance, False)
_DISTANCE_PROFILE = _TargetProfile(243, 27, 0, _encode_distance, False)
_WEIGHT_PROFILE = _TargetProfile(93, 1, 0, _encode_weight, False)
_SOUND_PRESSURE_PROFILE = _TargetProfile(243, 24, 0, _encode_sound_pressure, False)
_IRRADIANCE_PROFILE = _TargetProfile(243, 2, 0, _encode_irradiance, False)
_CARBON_DIOXIDE_PROFILE = _TargetProfile(249, 1, 0, _encode_carbon_dioxide, False)
_ALWAYS_CUSTOM_SEMANTICS = {
    "aqi",
    "energy",
    "volume_flow_rate",
}
_PROFILE_BY_SEMANTIC = {
    "temperature": (
        _TEMPERATURE_PROFILE,
        {"°C", "°F", "K", "celsius", "fahrenheit", "kelvin"},
    ),
    "humidity": (_HUMIDITY_PROFILE, {"%", "percent"}),
    "atmospheric_pressure": (_ATMOSPHERIC_PRESSURE_PROFILE, set(_HPA_FACTORS)),
    "pressure": (_PRESSURE_PROFILE, set(_BAR_FACTORS)),
    "voltage": (_VOLTAGE_PROFILE, set(_VOLT_FACTORS)),
    "current": (_CURRENT_PROFILE, set(_AMPERE_FACTORS)),
    "power": (_POWER_PROFILE, set(_WATT_FACTORS)),
    "illuminance": (_ILLUMINANCE_PROFILE, {"lx", "lux"}),
    "distance": (_DISTANCE_PROFILE, set(_CENTIMETER_FACTORS)),
    "weight": (_WEIGHT_PROFILE, set(_KILOGRAM_FACTORS)),
    "sound_pressure": (_SOUND_PRESSURE_PROFILE, {"dB", "dBA"}),
    "irradiance": (_IRRADIANCE_PROFILE, set(_IRRADIANCE_FACTORS)),
    "carbon_dioxide": (_CARBON_DIOXIDE_PROFILE, {"ppm"}),
}


def _target_profile(capability):
    """Choose a conservative native Domoticz profile or Custom fallback."""
    if getattr(capability, "state_class", None) not in {None, "measurement"}:
        return _CUSTOM_PROFILE

    semantic = capability.semantic
    unit = capability.unit
    if semantic in _ALWAYS_CUSTOM_SEMANTICS:
        return _CUSTOM_PROFILE

    profile_entry = _PROFILE_BY_SEMANTIC.get(semantic)
    if profile_entry is not None:
        profile, supported_units = profile_entry
        if unit in supported_units:
            return profile
        return _CUSTOM_PROFILE

    if unit in {"%", "percent"}:
        return _PERCENTAGE_PROFILE
    return _CUSTOM_PROFILE


class DomoticzSyncPlugin:
    """Callback-native connection client for the Home Assistant sync endpoint."""

    def __init__(self):
        self.connection = None
        self.phase = PHASE_STOPPED
        self._stopping = True
        self._address = None
        self._port = None
        self._transport_protocol = None
        self._link_id = None
        self._pairing_key = None
        self._destination_id = None
        self._upgrade_key = None
        self._offered_protocols = tuple(wire_protocol.SUPPORTED_WEBSOCKET_SUBPROTOCOLS)
        self._selected_protocol = None
        self._protocol_version = wire_protocol.PROTOCOL_VERSION
        self._protocol_selection = None
        self._hello = None
        self._handshake_context = None
        self._session_key = None
        self._session_id = None
        self._out_sequence = 0
        self._in_sequence = 0
        self._fragment_parts = []
        self._fragment_is_text = None
        self._fragment_size = 0
        self._heartbeat_tick = 0
        self._phase_started_tick = 0
        self._last_ping_tick = 0
        self._pending_ping_id = None
        self._pending_ping_tick = 0
        self._reconnect_delay = 1
        self._reconnect_remaining = 0

    def onStart(self):
        """Read configuration, establish identity, and start connecting."""
        try:
            self._read_parameters()
            self._destination_id = self._load_destination_id()
        except Exception:
            self.phase = PHASE_STOPPED
            self._stopping = True
            Domoticz.Error("Home Assistant sync configuration is invalid.")
            return

        self._stopping = False
        self.phase = PHASE_DISCONNECTED
        self._heartbeat_tick = 0
        self._reconnect_delay = 1
        self._reconnect_remaining = 0
        Domoticz.Heartbeat(_HEARTBEAT_SECONDS)
        self._connect()

    def _read_parameters(self):
        parameters = globals().get("Parameters")
        if not isinstance(parameters, dict):
            raise PluginConfigurationError

        address = parameters.get("Address")
        port = parameters.get("Port")
        transport_protocol = parameters.get("Mode1")
        link_id = parameters.get("Mode2")
        pairing_key = parameters.get("Mode3")
        if not all(
            isinstance(value, str)
            for value in (address, port, transport_protocol, link_id, pairing_key)
        ):
            raise PluginConfigurationError

        address = address.strip()
        port = port.strip()
        transport_protocol = transport_protocol.strip().upper()
        link_id = link_id.strip()
        pairing_key = pairing_key.strip()
        if (
            not address
            or any(char in address for char in ("/", "\r", "\n", "\t", " "))
            or not port.isdigit()
            or not 1 <= int(port) <= 65535
            or transport_protocol not in {"WS", "WSS"}
        ):
            raise PluginConfigurationError

        wire_protocol.validate_link_id(link_id)
        wire_protocol.validate_pairing_key(pairing_key)
        self._address = address
        self._port = port
        self._transport_protocol = transport_protocol
        self._link_id = link_id
        self._pairing_key = pairing_key

    def _load_destination_id(self):
        configuration = Domoticz.Configuration()
        if not isinstance(configuration, dict):
            raise PluginConfigurationError

        existing = configuration.get(_CONFIG_DESTINATION_ID)
        if existing is not None:
            return _canonical_destination_id(existing)

        destination_id = str(uuid.uuid4())
        wire_protocol.validate_destination_id(destination_id)
        updated = dict(configuration)
        updated[_CONFIG_DESTINATION_ID] = destination_id
        stored = Domoticz.Configuration(updated)
        if not isinstance(stored, dict):
            raise PluginConfigurationError
        return _canonical_destination_id(stored.get(_CONFIG_DESTINATION_ID))

    def _connect(self):
        if self._stopping:
            return
        if self.connection is not None:
            try:
                if self.connection.Connected() or self.connection.Connecting():
                    return
            except Exception:
                pass

        self._reset_session()
        self.phase = PHASE_CONNECTING
        self._phase_started_tick = self._heartbeat_tick
        try:
            connection = Domoticz.Connection(
                Name="Home Assistant Sync",
                Transport="TCP/IP",
                Protocol=self._transport_protocol,
                Address=self._address,
                Port=self._port,
            )
            self.connection = connection
            connection.Connect()
        except Exception:
            self.connection = None
            Domoticz.Error("Could not connect to Home Assistant.")
            self._schedule_reconnect()

    def onConnect(self, connection, status, description):
        """Upgrade a successfully opened transport to WebSocket."""
        del description
        if connection is not self.connection or self._stopping:
            return
        if status != 0:
            self.connection = None
            Domoticz.Error("Could not connect to Home Assistant.")
            self._schedule_reconnect()
            return

        self._upgrade_key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        scheme = "https" if self._transport_protocol == "WSS" else "http"
        host = self._host_header()
        request = {
            "URL": _ENDPOINT,
            "Headers": {
                "Host": host,
                "Origin": f"{scheme}://{host}",
                "Sec-WebSocket-Key": self._upgrade_key,
                "Sec-WebSocket-Protocol": ", ".join(self._offered_protocols),
                # Domoticz otherwise advertises compression that it cannot decode.
                "Sec-WebSocket-Extensions": None,
            },
        }
        self.phase = PHASE_UPGRADING
        self._phase_started_tick = self._heartbeat_tick
        try:
            connection.Send(request)
        except Exception:
            self._reject_connection("The WebSocket upgrade could not be sent.")

    def _host_header(self):
        address = self._address
        if ":" in address and not address.startswith("["):
            address = f"[{address}]"
        default_port = "443" if self._transport_protocol == "WSS" else "80"
        if self._port == default_port:
            return address
        return f"{address}:{self._port}"

    def onMessage(self, connection, data):
        """Process HTTP upgrade, WebSocket control, and protocol messages."""
        if connection is not self.connection or self._stopping:
            return
        if not isinstance(data, dict):
            self._reject_connection("An invalid WebSocket message was received.")
            return

        if "Status" in data:
            self._handle_upgrade(data)
            return
        if "Operation" in data:
            self._handle_control(data)
            return
        if "Payload" in data:
            self._handle_data(data)
            return
        self._reject_connection("An invalid WebSocket message was received.")

    def _handle_upgrade(self, data):
        if self.phase != PHASE_UPGRADING or str(data.get("Status")) != "101":
            self._reject_connection("The WebSocket upgrade was rejected.")
            return

        headers = data.get("Headers")
        expected_accept = base64.b64encode(
            hashlib.sha1((self._upgrade_key + _WEBSOCKET_GUID).encode("ascii")).digest()
        ).decode("ascii")
        actual_accept = _header_value(headers, "Sec-WebSocket-Accept")
        upgrade = _header_value(headers, "Upgrade")
        connection_header = _header_value(headers, "Connection")
        extensions = _header_value(headers, "Sec-WebSocket-Extensions")
        selected_protocol_headers = _header_count(
            headers,
            "Sec-WebSocket-Protocol",
        )
        selected_protocol = _header_value(headers, "Sec-WebSocket-Protocol")
        if (
            not isinstance(actual_accept, str)
            or not hmac.compare_digest(actual_accept, expected_accept)
            or not isinstance(upgrade, str)
            or upgrade.lower() != "websocket"
            or not isinstance(connection_header, str)
            or "upgrade"
            not in {part.strip().lower() for part in connection_header.split(",")}
            or (isinstance(extensions, str) and extensions.strip())
        ):
            self._reject_connection("The WebSocket upgrade response was invalid.")
            return
        if selected_protocol_headers > 1 or (
            selected_protocol_headers == 1
            and (
                selected_protocol is None
                or selected_protocol not in self._offered_protocols
                or selected_protocol != wire_protocol.WEBSOCKET_SUBPROTOCOL_V2
            )
        ):
            self._reject_connection("The WebSocket protocol selection was invalid.")
            return

        self._upgrade_key = None
        try:
            client_nonce = wire_protocol.generate_nonce()
            self._selected_protocol = selected_protocol
            if selected_protocol is None:
                self._protocol_version = wire_protocol.PROTOCOL_VERSION
                hello_document = wire_protocol.build_hello(
                    self._link_id,
                    self._destination_id,
                    client_nonce,
                )
                self._hello = wire_protocol.parse_hello(hello_document)
            else:
                self._protocol_version = wire_protocol.PROTOCOL_VERSION_V2
                hello_document = wire_protocol.build_v2_hello(
                    self._link_id,
                    self._destination_id,
                    client_nonce,
                    client_protocols=self._offered_protocols,
                    selected_protocol=selected_protocol,
                    client_features=wire_protocol.SUPPORTED_V2_FEATURES,
                )
                self._hello = wire_protocol.parse_v2_hello(hello_document)
            self.phase = PHASE_WAIT_CHALLENGE
            self._phase_started_tick = self._heartbeat_tick
            self._send_document(hello_document)
        except Exception:
            self._reject_connection("The secure pairing handshake could not start.")

    def _handle_data(self, data):
        payload = data.get("Payload")
        finish = data.get("Finish", True)
        if not isinstance(finish, bool) or not isinstance(
            payload, (str, bytes, bytearray)
        ):
            self._reject_connection("An invalid WebSocket data message was received.")
            return

        is_text = isinstance(payload, str)
        if self._fragment_is_text is None:
            if not is_text:
                self._reject_connection(
                    "Binary or compressed messages are not supported."
                )
                return
            self._fragment_is_text = True

        try:
            chunk = payload.encode("utf-8") if is_text else bytes(payload)
        except (UnicodeError, ValueError):
            self._reject_connection("An invalid WebSocket data message was received.")
            return
        self._fragment_parts.append(chunk)
        self._fragment_size += len(chunk)
        if self._fragment_size > _MAX_MESSAGE_BYTES:
            self._reject_connection("A WebSocket message exceeded the size limit.")
            return
        if not finish:
            return

        complete_bytes = b"".join(self._fragment_parts)
        self._reset_fragments()
        try:
            complete = complete_bytes.decode("utf-8")
        except UnicodeDecodeError:
            self._reject_connection(
                "An invalid fragmented WebSocket message was received."
            )
            return

        try:
            document = wire_protocol.canonical_json_loads(complete)
            self._handle_protocol_document(document)
        except Exception:
            self._reject_connection("A secure protocol message was rejected.")

    def _handle_protocol_document(self, document):
        if self.phase == PHASE_WAIT_CHALLENGE:
            if self._protocol_version == wire_protocol.PROTOCOL_VERSION_V2:
                context = wire_protocol.accept_v2_challenge(
                    self._pairing_key,
                    self._hello,
                    document,
                )
                self._protocol_selection = context.selection
                session_key = wire_protocol.derive_v2_session_key(
                    self._pairing_key,
                    context,
                )
                authenticate = wire_protocol.build_v2_authenticate(
                    self._pairing_key,
                    context,
                )
            else:
                context = wire_protocol.accept_challenge(
                    self._pairing_key,
                    self._hello,
                    document,
                )
                session_key = wire_protocol.derive_session_key(
                    self._pairing_key,
                    context,
                )
                authenticate = wire_protocol.build_authenticate(
                    self._pairing_key,
                    context,
                )
            self._handshake_context = context
            self._session_key = session_key
            self.phase = PHASE_WAIT_PROTOCOL_READY
            self._phase_started_tick = self._heartbeat_tick
            self._send_document(authenticate)
            return

        if self.phase == PHASE_WAIT_PROTOCOL_READY:
            if self._protocol_version == wire_protocol.PROTOCOL_VERSION_V2:
                self._session_id = wire_protocol.verify_v2_ready(
                    self._session_key,
                    self._handshake_context,
                    document,
                )
                initial_payload = wire_protocol.build_application_ready(
                    self._protocol_selection
                )
            else:
                self._session_id = wire_protocol.verify_ready(
                    self._session_key,
                    self._handshake_context,
                    document,
                )
                initial_payload = {"type": "inventory", "targets": []}
            self._out_sequence = 0
            self._in_sequence = 0
            self.phase = PHASE_WAIT_APPLICATION_READY
            self._phase_started_tick = self._heartbeat_tick
            self._send_signed(initial_payload)
            return

        if self.phase not in {PHASE_WAIT_APPLICATION_READY, PHASE_READY}:
            raise wire_protocol.ProtocolFormatError

        verified = wire_protocol.verify_envelope(
            self._session_key,
            document,
            protocol_version=self._protocol_version,
            expected_direction=wire_protocol.DIRECTION_HA_TO_DOMOTICZ,
            expected_session_id=self._session_id,
            last_sequence=self._in_sequence,
        )
        self._in_sequence = verified.sequence
        payload = verified.payload
        if self.phase == PHASE_WAIT_APPLICATION_READY:
            if self._protocol_version == wire_protocol.PROTOCOL_VERSION_V2:
                wire_protocol.parse_application_ready(
                    self._protocol_selection,
                    payload,
                )
            elif payload != {"type": "ready"}:
                raise wire_protocol.ProtocolFormatError
            self.phase = PHASE_READY
            self._reconnect_delay = 1
            self._last_ping_tick = self._heartbeat_tick
            if self._protocol_version == wire_protocol.PROTOCOL_VERSION:
                Domoticz.Status(
                    "Authenticated Home Assistant connection is ready in "
                    "v1 compatibility mode; protocol=v1; features=none; "
                    "entity export is disabled."
                )
            else:
                features = ",".join(self._protocol_selection.features) or "none"
                Domoticz.Status(
                    "Authenticated Home Assistant connection is ready; "
                    f"protocol={self._selected_protocol}; features={features}."
                )
            return

        self._handle_signed_payload(payload)

    def _handle_signed_payload(self, payload):
        if not isinstance(payload, dict):
            raise wire_protocol.ProtocolFormatError
        message_type = payload.get("type")
        if message_type == "apply":
            if (
                self._protocol_selection is None
                or not self._protocol_selection.supports(
                    wire_protocol.FEATURE_HA_EXPORT_NUMERIC_V1
                )
            ):
                raise wire_protocol.ProtocolCompatibilityError
            self._handle_apply_payload(payload)
            return

        message_id = payload.get("id")
        if set(payload) != {"type", "id"}:
            raise wire_protocol.ProtocolFormatError
        wire_protocol.validate_nonce(message_id)
        if message_type == "ping":
            self._send_signed({"type": "pong", "id": message_id})
            return
        if message_type == "pong" and message_id == self._pending_ping_id:
            self._pending_ping_id = None
            self._last_ping_tick = self._heartbeat_tick
            return
        raise wire_protocol.ProtocolFormatError

    def _handle_apply_payload(self, payload):
        """Apply one correlated request and return only a sanitized result."""
        request = wire_protocol.parse_apply(self._protocol_selection, payload)

        try:
            target_id = self._apply_action(request.action)
        except Exception:
            result = wire_protocol.build_apply_result(
                self._protocol_selection,
                request.request_id,
                wire_protocol.ApplyResultStatus.REJECTED,
                None,
                None,
            )
        else:
            result = wire_protocol.build_apply_result(
                self._protocol_selection,
                request.request_id,
                wire_protocol.ApplyResultStatus.CONFIRMED,
                target_id,
                request.action.capability.source,
            )
        self._send_signed(result)

    def _apply_action(self, action):
        """Idempotently converge and re-read one numeric Domoticz target."""
        capability = action.capability
        if capability.kind.value != "numeric":
            raise DomoticzApplyError

        action_kind = action.kind.value
        if action_kind not in {"create", "update", "mark_unavailable"}:
            raise DomoticzApplyError

        device_id = _device_id_for_source(capability.source)
        if action_kind != "create" and action.target_id != device_id:
            raise DomoticzApplyError

        available = capability.availability.value == "available"
        profile = _target_profile(capability)
        desired_values = (
            profile.encoder(capability.value, capability.unit) if available else None
        )
        options = (
            _custom_sensor_options(capability.unit) if profile.manages_options else None
        )
        device = self._get_device(device_id)
        unit = self._get_unit(device)

        if unit is None:
            if action_kind != "create" and not (action_kind == "update" and available):
                raise DomoticzApplyError
            create_arguments = {
                "Name": capability.name,
                "DeviceID": device_id,
                "Unit": _TARGET_UNIT,
                "Type": profile.type_id,
                "Subtype": profile.subtype,
                "Switchtype": profile.switch_type,
                "Used": 1,
            }
            if profile.manages_options:
                create_arguments["Options"] = options
            Domoticz.Unit(**create_arguments).Create()
            device = self._get_device(device_id)
            unit = self._get_unit(device)

        if not self._is_target_profile(unit, profile):
            raise DomoticzApplyError

        self._converge_target(
            device,
            unit,
            profile=profile,
            name=capability.name,
            options=options,
            available=available,
            values=desired_values,
        )
        confirmed_device = self._get_device(device_id)
        confirmed_unit = self._get_unit(confirmed_device)
        if confirmed_unit is None:
            raise DomoticzApplyError
        confirmed_unit.Refresh()
        confirmed_device = self._get_device(device_id)
        confirmed_unit = self._get_unit(confirmed_device)
        if not self._target_matches(
            confirmed_device,
            confirmed_unit,
            profile=profile,
            name=capability.name,
            options=options,
            available=available,
            values=desired_values,
        ):
            raise DomoticzApplyError
        return device_id

    @staticmethod
    def _get_device(device_id):
        """Read a device container from Domoticz's current registry."""
        devices = globals().get("Devices")
        if devices is None or device_id not in devices:
            return None
        return devices[device_id]

    @staticmethod
    def _get_unit(device):
        """Read Unit 1 from a current extended device container."""
        if device is None:
            return None
        units = getattr(device, "Units", None)
        if units is None or _TARGET_UNIT not in units:
            return None
        return units[_TARGET_UNIT]

    @staticmethod
    def _is_target_profile(unit, profile):
        return (
            unit is not None
            and getattr(unit, "Type", None) == profile.type_id
            and getattr(unit, "SubType", None) == profile.subtype
            and getattr(unit, "SwitchType", None) == profile.switch_type
        )

    @staticmethod
    def _converge_target(
        device,
        unit,
        *,
        profile,
        name,
        options,
        available,
        values,
    ):
        """Set the complete desired state while retaining unavailable values."""
        properties_changed = (
            getattr(unit, "Name", None) != name or getattr(unit, "Used", None) != 1
        )
        options_changed = (
            profile.manages_options and getattr(unit, "Options", None) != options
        )
        timed_out = 0 if available else 1
        timeout_changed = getattr(device, "TimedOut", None) != timed_out
        values_changed = False
        if available:
            n_value, s_value = values
            values_changed = (
                getattr(unit, "nValue", None) != n_value
                or getattr(unit, "sValue", None) != s_value
            )
        if (
            not properties_changed
            and not options_changed
            and not values_changed
            and not timeout_changed
        ):
            return

        unit.Name = name
        unit.Used = 1
        if profile.manages_options:
            unit.Options = dict(options)
        # Extended Domoticz exposes timeout on the parent Device. It is
        # runtime-only state read directly by CPlugin::HasNodeFailed.
        device.TimedOut = timed_out
        if available:
            unit.nValue = n_value
            unit.sValue = s_value

        if properties_changed or options_changed or values_changed:
            update = {"Log": False}
            if properties_changed or options_changed:
                update["UpdateProperties"] = True
            if options_changed:
                update["UpdateOptions"] = True
            unit.Update(**update)

    @classmethod
    def _target_matches(
        cls,
        device,
        unit,
        *,
        profile,
        name,
        options,
        available,
        values,
    ):
        """Confirm desired state from a fresh registry lookup."""
        if (
            not cls._is_target_profile(unit, profile)
            or getattr(unit, "Name", None) != name
            or getattr(unit, "Used", None) != 1
            or getattr(device, "TimedOut", None) != (0 if available else 1)
        ):
            return False
        if profile.manages_options and getattr(unit, "Options", None) != options:
            return False
        if not available:
            return True
        n_value, s_value = values
        return (
            getattr(unit, "nValue", None) == n_value
            and getattr(unit, "sValue", None) == s_value
        )

    def _send_document(self, document):
        if self.connection is None:
            raise RuntimeError
        payload = wire_protocol.canonical_json_dumps(document)
        self.connection.Send({"Payload": payload, "Mask": secrets.randbits(32)})

    def _send_signed(self, payload):
        sequence = self._out_sequence + 1
        document = wire_protocol.sign_envelope(
            self._session_key,
            protocol_version=self._protocol_version,
            direction=wire_protocol.DIRECTION_DOMOTICZ_TO_HA,
            session_id=self._session_id,
            sequence=sequence,
            payload=payload,
        )
        self._send_document(document)
        self._out_sequence = sequence

    def _handle_control(self, data):
        operation = data.get("Operation")
        if operation == "Ping":
            payload = data.get("Payload", b"")
            if isinstance(payload, list):
                try:
                    payload = bytes(payload)
                except (TypeError, ValueError):
                    self._reject_connection("An invalid WebSocket ping was received.")
                    return
            if not isinstance(payload, (str, bytes, bytearray)):
                self._reject_connection("An invalid WebSocket ping was received.")
                return
            try:
                self.connection.Send(
                    {
                        "Operation": "Pong",
                        "Payload": payload,
                        "Mask": secrets.randbits(32),
                    }
                )
            except Exception:
                self._reject_connection("The WebSocket pong could not be sent.")
            return
        if operation == "Pong":
            return
        if operation == "Close":
            self._reject_connection("Home Assistant closed the sync connection.")
            return
        self._reject_connection(
            "An unsupported WebSocket control message was received."
        )

    def onHeartbeat(self):
        """Drive reconnects, timeouts, and signed keepalives."""
        if self._stopping:
            return
        self._heartbeat_tick += 1

        if self.phase == PHASE_DISCONNECTED:
            if self._reconnect_remaining > 0:
                self._reconnect_remaining -= 1
            if self._reconnect_remaining == 0:
                self._connect()
            return

        if self.phase == PHASE_CONNECTING:
            if (
                self._heartbeat_tick - self._phase_started_tick
                >= _CONNECT_TIMEOUT_TICKS
            ):
                self._reject_connection(
                    "The connection attempt timed out.", send_close=False
                )
            return

        if self.phase == PHASE_UPGRADING:
            if (
                self._heartbeat_tick - self._phase_started_tick
                >= _HANDSHAKE_TIMEOUT_TICKS
            ):
                self._reject_connection("The WebSocket upgrade timed out.")
            return

        if self.phase in {
            PHASE_WAIT_CHALLENGE,
            PHASE_WAIT_PROTOCOL_READY,
            PHASE_WAIT_APPLICATION_READY,
        }:
            if (
                self._heartbeat_tick - self._phase_started_tick
                >= _HANDSHAKE_TIMEOUT_TICKS
            ):
                self._reject_connection("The secure pairing handshake timed out.")
            return

        if self.phase != PHASE_READY:
            return
        if self._pending_ping_id is not None:
            if self._heartbeat_tick - self._pending_ping_tick >= _PING_TIMEOUT_TICKS:
                self._reject_connection("The signed heartbeat timed out.")
            return
        if self._heartbeat_tick - self._last_ping_tick >= _PING_INTERVAL_TICKS:
            ping_id = wire_protocol.generate_nonce()
            try:
                self._send_signed({"type": "ping", "id": ping_id})
            except Exception:
                self._reject_connection("The signed heartbeat could not be sent.")
                return
            self._pending_ping_id = ping_id
            self._pending_ping_tick = self._heartbeat_tick

    def onDisconnect(self, connection):
        """Schedule reconnect after a transport loss."""
        if connection is not self.connection:
            return
        self.connection = None
        if self._stopping:
            self.phase = PHASE_STOPPED
            return
        Domoticz.Error("The Home Assistant sync connection was lost.")
        self._schedule_reconnect()

    def onStop(self):
        """Close the connection without scheduling another attempt."""
        self._stopping = True
        self.phase = PHASE_STOPPED
        connection = self.connection
        self.connection = None
        self._reset_session()
        if connection is not None:
            try:
                connection.Send({"Operation": "Close", "Mask": secrets.randbits(32)})
            except Exception:
                pass
            try:
                connection.Disconnect()
            except Exception:
                pass

    def _reject_connection(self, message, *, send_close=True):
        Domoticz.Error(message)
        connection = self.connection
        self.connection = None
        self._schedule_reconnect()
        if connection is not None:
            if send_close:
                try:
                    connection.Send(
                        {"Operation": "Close", "Mask": secrets.randbits(32)}
                    )
                except Exception:
                    pass
            try:
                connection.Disconnect()
            except Exception:
                pass

    def _schedule_reconnect(self):
        if self._stopping:
            self.phase = PHASE_STOPPED
            return
        self._reset_session()
        self.phase = PHASE_DISCONNECTED
        self._reconnect_remaining = self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, _MAX_RECONNECT_TICKS)

    def _reset_session(self):
        self._upgrade_key = None
        self._selected_protocol = None
        self._protocol_version = wire_protocol.PROTOCOL_VERSION
        self._protocol_selection = None
        self._hello = None
        self._handshake_context = None
        self._session_key = None
        self._session_id = None
        self._out_sequence = 0
        self._in_sequence = 0
        self._pending_ping_id = None
        self._reset_fragments()

    def _reset_fragments(self):
        self._fragment_parts = []
        self._fragment_is_text = None
        self._fragment_size = 0


_plugin = DomoticzSyncPlugin()


def _callback(name, function, *args):
    try:
        return function(*args)
    except Exception:
        Domoticz.Error(f"Internal error in the {name} callback.")
        return None


def onStart():
    return _callback("start", _plugin.onStart)


def onStop():
    return _callback("stop", _plugin.onStop)


def onConnect(Connection, Status, Description):
    return _callback("connect", _plugin.onConnect, Connection, Status, Description)


def onMessage(Connection, Data):
    return _callback("message", _plugin.onMessage, Connection, Data)


def onDisconnect(Connection):
    return _callback("disconnect", _plugin.onDisconnect, Connection)


def onHeartbeat():
    return _callback("heartbeat", _plugin.onHeartbeat)
