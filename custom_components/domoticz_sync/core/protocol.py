"""Authenticated wire protocol shared by Home Assistant and Domoticz.

The module is deliberately host-neutral and uses only Python 3.9-compatible
standard-library features so the Domoticz plugin can vendor it unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .capabilities import Availability, Capability, CapabilityKind, SourceIdentity
from .reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
    derive_domoticz_target_id,
)

PROTOCOL_VERSION_V1 = 1
# Backward-compatible public name retained for the frozen legacy v1 codec.
# It deliberately does not mean "latest protocol version."
PROTOCOL_VERSION = PROTOCOL_VERSION_V1
PROTOCOL_VERSION_V2 = 2

WEBSOCKET_SUBPROTOCOL_V2 = "ha-domoticz-sync.v2"
FEATURE_DOMOTICZ_INVENTORY_V1 = "domoticz-inventory.v1"
FEATURE_HA_EXPORT_BINARY_V1 = "ha-export.binary.v1"
FEATURE_HA_EXPORT_NUMERIC_V1 = "ha-export.numeric.v1"
SUPPORTED_WEBSOCKET_SUBPROTOCOLS = (WEBSOCKET_SUBPROTOCOL_V2,)
SUPPORTED_V2_FEATURES = (
    FEATURE_HA_EXPORT_BINARY_V1,
    FEATURE_HA_EXPORT_NUMERIC_V1,
)

DIRECTION_DOMOTICZ_TO_HA = "domoticz_to_home_assistant"
DIRECTION_HA_TO_DOMOTICZ = "home_assistant_to_domoticz"

PAIRING_KEY_BITS = 256
NONCE_BITS = 256
MAX_MESSAGE_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_SEQUENCE = MAX_SAFE_INTEGER
MAX_PROTOCOL_TOKENS = 16
MAX_FEATURE_IDS = 64
MAX_INVENTORY_TARGETS = 512
MAX_INVENTORY_UNITS = 1024
MAX_INVENTORY_PAGES = 512
MAX_INVENTORY_TARGETS_PER_PAGE = 64
MAX_INVENTORY_PAYLOAD_BYTES = 60 * 1024
INVENTORY_TIMEOUT_SECONDS = 10
MAX_INVENTORY_TARGET_ID_BYTES = 128
MAX_INVENTORY_NAME_BYTES = 512
MAX_INVENTORY_S_VALUE_BYTES = 4096
MAX_INVENTORY_OPTION_BYTES = 1024

_SECRET_BYTES = PAIRING_KEY_BITS // 8
_NONCE_BYTES = NONCE_BITS // 8
_TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_WEBSOCKET_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,128}$")
_TOKEN_DECODE_ERRORS = (UnicodeError, TypeError, ValueError)
_DIRECTIONS = {
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
}

_HELLO_KEYS = {"version", "type", "link_id", "destination_id", "client_nonce"}
_CHALLENGE_KEYS = {"version", "type", "server_nonce", "server_proof"}
_AUTHENTICATE_KEYS = {"version", "type", "client_proof"}
_READY_KEYS = {"version", "type", "session_id"}
_V2_HELLO_KEYS = {
    "version",
    "type",
    "link_id",
    "destination_id",
    "client_nonce",
    "client_protocols",
    "selected_protocol",
    "client_features",
}
_V2_CHALLENGE_KEYS = {
    "version",
    "type",
    "server_nonce",
    "server_protocols",
    "selected_protocol",
    "server_features",
    "selected_features",
    "server_proof",
}
_APPLICATION_READY_KEYS = {"schema", "type"}
_APPLY_KEYS = {"schema", "type", "request_id", "action"}
_APPLY_RESULT_KEYS = {
    "schema",
    "type",
    "request_id",
    "status",
    "target_id",
    "source",
}
_INVENTORY_REQUEST_KEYS = {"schema", "type", "request_id"}
_INVENTORY_RESULT_KEYS = {
    "schema",
    "type",
    "request_id",
    "status",
    "page",
    "complete",
    "targets",
}
_INVENTORY_TARGET_KEYS = {"target_id", "timed_out", "units"}
_INVENTORY_UNIT_KEYS = {
    "unit",
    "name",
    "type",
    "subtype",
    "switch_type",
    "used",
    "n_value",
    "s_value",
    "custom_option",
    "has_other_options",
}
_ACTION_KEYS = {"kind", "capability", "target_id", "stale"}
_CAPABILITY_KEYS = {
    "source",
    "kind",
    "name",
    "value",
    "availability",
    "semantic",
    "unit",
    "state_class",
}
_SOURCE_KEYS = {"system", "instance_id", "object_id", "capability_id"}
_ENVELOPE_KEYS = {
    "version",
    "type",
    "session_id",
    "direction",
    "sequence",
    "payload",
    "signature",
}

_PROTOCOL_DOMAIN = b"ha-domoticz-sync/protocol/v1/"
_CLIENT_PROOF_DOMAIN = _PROTOCOL_DOMAIN + b"client-proof\x00"
_SERVER_PROOF_DOMAIN = _PROTOCOL_DOMAIN + b"server-proof\x00"
_SESSION_SALT_DOMAIN = _PROTOCOL_DOMAIN + b"session-salt\x00"
_SESSION_KEY_DOMAIN = _PROTOCOL_DOMAIN + b"session-key\x00"
_SESSION_ID_DOMAIN = _PROTOCOL_DOMAIN + b"session-id\x00"
_ENVELOPE_DOMAIN = _PROTOCOL_DOMAIN + b"envelope\x00"

_V2_PROTOCOL_DOMAIN = b"ha-domoticz-sync/protocol/v2/"
_V2_CLIENT_PROOF_DOMAIN = _V2_PROTOCOL_DOMAIN + b"client-proof\x00"
_V2_SERVER_PROOF_DOMAIN = _V2_PROTOCOL_DOMAIN + b"server-proof\x00"
_V2_SESSION_SALT_DOMAIN = _V2_PROTOCOL_DOMAIN + b"session-salt\x00"
_V2_SESSION_KEY_DOMAIN = _V2_PROTOCOL_DOMAIN + b"session-key\x00"
_V2_SESSION_ID_DOMAIN = _V2_PROTOCOL_DOMAIN + b"session-id\x00"
_V2_ENVELOPE_DOMAIN = _V2_PROTOCOL_DOMAIN + b"envelope\x00"


class ProtocolError(ValueError):
    """Base class for safe protocol failures."""


class ProtocolFormatError(ProtocolError):
    """A protocol document or value does not match its selected format."""


class ProtocolAuthenticationError(ProtocolError):
    """A proof, signature, or authenticated session value is invalid."""


class ProtocolSequenceError(ProtocolError):
    """An authenticated envelope is replayed, missing, or out of order."""


class ProtocolCompatibilityError(ProtocolError):
    """The peers have no mutually supported authenticated behavior."""


class ApplyResultStatus(str, Enum):
    """The only safe outcomes returned for one remote action."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class InventoryResultStatus(str, Enum):
    """The only safe outcomes for one authenticated inventory request."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ClientHello:
    """The identity and fresh nonce supplied by the Domoticz client."""

    link_id: str
    destination_id: str
    client_nonce: str

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as parsed input."""
        validate_link_id(self.link_id)
        validate_destination_id(self.destination_id)
        validate_nonce(self.client_nonce)


@dataclass(frozen=True)
class HandshakeContext:
    """All public values bound into mutual authentication and key derivation."""

    link_id: str
    destination_id: str
    client_nonce: str
    server_nonce: str

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as parsed input."""
        validate_link_id(self.link_id)
        validate_destination_id(self.destination_id)
        validate_nonce(self.client_nonce)
        validate_nonce(self.server_nonce)


@dataclass(frozen=True)
class ProtocolSelection:
    """One authenticated wire version and its negotiated optional features."""

    version: int
    websocket_subprotocol: str
    features: Tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as handshake selection."""
        if self.version != PROTOCOL_VERSION_V2:
            raise ProtocolFormatError("invalid protocol message")
        if self.websocket_subprotocol != WEBSOCKET_SUBPROTOCOL_V2:
            raise ProtocolFormatError("invalid protocol message")
        if type(self.features) is not tuple:
            raise ProtocolFormatError("invalid protocol message")
        validate_feature_ids(self.features)

    def supports(self, feature: str) -> bool:
        """Return whether one optional behavior was mutually negotiated."""
        _validate_feature_id(feature)
        return feature in self.features


@dataclass(frozen=True)
class V2ClientHello:
    """The authenticated repeat of the HTTP WebSocket negotiation offer."""

    link_id: str
    destination_id: str
    client_nonce: str
    client_protocols: Tuple[str, ...]
    selected_protocol: str
    client_features: Tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as parsed input."""
        validate_link_id(self.link_id)
        validate_destination_id(self.destination_id)
        validate_nonce(self.client_nonce)
        if (
            type(self.client_protocols) is not tuple
            or type(self.client_features) is not tuple
        ):
            raise ProtocolFormatError("invalid protocol message")
        protocols = validate_protocol_tokens(self.client_protocols)
        if (
            self.selected_protocol != WEBSOCKET_SUBPROTOCOL_V2
            or self.selected_protocol not in protocols
        ):
            raise ProtocolFormatError("invalid protocol message")
        validate_feature_ids(self.client_features)


@dataclass(frozen=True)
class V2HandshakeContext:
    """Every public v2 negotiation value bound into authentication and KDF."""

    link_id: str
    destination_id: str
    client_nonce: str
    server_nonce: str
    client_protocols: Tuple[str, ...]
    server_protocols: Tuple[str, ...]
    selected_protocol: str
    client_features: Tuple[str, ...]
    server_features: Tuple[str, ...]
    selected_features: Tuple[str, ...]

    def __post_init__(self) -> None:
        """Require one complete deterministic negotiation transcript."""
        validate_link_id(self.link_id)
        validate_destination_id(self.destination_id)
        validate_nonce(self.client_nonce)
        validate_nonce(self.server_nonce)
        if any(
            type(value) is not tuple
            for value in (
                self.client_protocols,
                self.server_protocols,
                self.client_features,
                self.server_features,
                self.selected_features,
            )
        ):
            raise ProtocolFormatError("invalid protocol message")
        expected_protocol = select_websocket_subprotocol(
            self.client_protocols,
            self.server_protocols,
        )
        if (
            self.selected_protocol != WEBSOCKET_SUBPROTOCOL_V2
            or self.selected_protocol != expected_protocol
        ):
            raise ProtocolFormatError("invalid protocol message")
        expected_features = negotiate_features(
            self.client_features,
            self.server_features,
        )
        if self.selected_features != expected_features:
            raise ProtocolFormatError("invalid protocol message")

    @property
    def selection(self) -> ProtocolSelection:
        """Return the immutable application contract selected by this context."""
        return ProtocolSelection(
            version=PROTOCOL_VERSION_V2,
            websocket_subprotocol=self.selected_protocol,
            features=self.selected_features,
        )


@dataclass(frozen=True)
class VerifiedEnvelope:
    """One authenticated, in-order application payload."""

    session_id: str
    direction: str
    sequence: int
    payload: Dict[str, object]


@dataclass(frozen=True)
class ApplyRequest:
    """One correlation identifier and complete target-neutral action."""

    request_id: str
    action: ReconciliationAction

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as parsed input."""
        _validate_request_id(self.request_id)
        if not isinstance(self.action, ReconciliationAction):
            raise ProtocolFormatError("invalid protocol message")


@dataclass(frozen=True)
class ApplyResult:
    """A sanitized remote confirmation or rejection."""

    request_id: str
    status: ApplyResultStatus
    target_id: Optional[str]
    source: Optional[SourceIdentity]

    def __post_init__(self) -> None:
        """Require result fields to agree with their status."""
        _validate_request_id(self.request_id)
        if not isinstance(self.status, ApplyResultStatus):
            raise ProtocolFormatError("invalid protocol message")
        if self.status is ApplyResultStatus.CONFIRMED:
            _validate_target_id(self.target_id)
            if not isinstance(self.source, SourceIdentity):
                raise ProtocolFormatError("invalid protocol message")
        elif self.target_id is not None or self.source is not None:
            raise ProtocolFormatError("invalid protocol message")


@dataclass(frozen=True)
class InventoryUnit:
    """One bounded Domoticz unit observation inside an inventory snapshot."""

    unit: int
    name: str
    type: int
    subtype: int
    switch_type: int
    used: bool
    n_value: int
    s_value: str
    custom_option: Optional[str]
    has_other_options: bool

    def __post_init__(self) -> None:
        """Validate direct construction as strictly as parsed input."""
        _validate_bounded_integer(self.unit, 1, 255)
        _validate_inventory_string(self.name, MAX_INVENTORY_NAME_BYTES)
        _validate_bounded_integer(self.type, 0, MAX_SAFE_INTEGER)
        _validate_bounded_integer(self.subtype, 0, MAX_SAFE_INTEGER)
        _validate_bounded_integer(self.switch_type, 0, MAX_SAFE_INTEGER)
        _validate_strict_bool(self.used)
        _validate_bounded_integer(
            self.n_value,
            -MAX_SAFE_INTEGER,
            MAX_SAFE_INTEGER,
        )
        _validate_inventory_string(self.s_value, MAX_INVENTORY_S_VALUE_BYTES)
        if self.custom_option is not None:
            _validate_inventory_string(
                self.custom_option,
                MAX_INVENTORY_OPTION_BYTES,
            )
        _validate_strict_bool(self.has_other_options)


@dataclass(frozen=True)
class InventoryTarget:
    """One hardware-scoped Domoticz parent and its ordered units."""

    target_id: str
    timed_out: bool
    units: Tuple[InventoryUnit, ...]

    def __post_init__(self) -> None:
        """Require a deterministic, duplicate-free unit ordering."""
        _validate_inventory_target_id(self.target_id)
        _validate_strict_bool(self.timed_out)
        if type(self.units) is not tuple or len(self.units) > MAX_INVENTORY_UNITS:
            raise ProtocolFormatError("invalid protocol message")
        unit_numbers: List[int] = []
        for unit in self.units:
            if not isinstance(unit, InventoryUnit):
                raise ProtocolFormatError("invalid protocol message")
            unit_numbers.append(unit.unit)
        if unit_numbers != sorted(unit_numbers) or len(unit_numbers) != len(
            set(unit_numbers)
        ):
            raise ProtocolFormatError("invalid protocol message")


@dataclass(frozen=True)
class InventoryResult:
    """One page of an authenticated, bounded Domoticz inventory snapshot."""

    request_id: str
    status: InventoryResultStatus
    page: int
    complete: bool
    targets: Tuple[InventoryTarget, ...]

    def __post_init__(self) -> None:
        """Require one exact confirmed page or sanitized rejection."""
        _validate_request_id(self.request_id)
        if not isinstance(self.status, InventoryResultStatus):
            raise ProtocolFormatError("invalid protocol message")
        _validate_bounded_integer(self.page, 1, MAX_INVENTORY_PAGES)
        _validate_strict_bool(self.complete)
        if (
            type(self.targets) is not tuple
            or len(self.targets) > MAX_INVENTORY_TARGETS_PER_PAGE
        ):
            raise ProtocolFormatError("invalid protocol message")

        target_ids: List[str] = []
        for target in self.targets:
            if not isinstance(target, InventoryTarget):
                raise ProtocolFormatError("invalid protocol message")
            target_ids.append(target.target_id)
        if target_ids != sorted(target_ids) or len(target_ids) != len(
            set(target_ids)
        ):
            raise ProtocolFormatError("invalid protocol message")

        if self.status is InventoryResultStatus.REJECTED:
            if self.page != 1 or not self.complete or self.targets:
                raise ProtocolFormatError("invalid protocol message")
        elif not self.targets and (self.page != 1 or not self.complete):
            raise ProtocolFormatError("invalid protocol message")
        elif self.page == MAX_INVENTORY_PAGES and not self.complete:
            raise ProtocolFormatError("invalid protocol message")


def canonical_json_dumps(value: object) -> str:
    """Serialize one value to the protocol's deterministic JSON subset."""
    try:
        _validate_json_value(value)
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("ascii")) > MAX_MESSAGE_BYTES:
            raise ValueError
        return encoded
    except (
        UnicodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        raise ProtocolFormatError("invalid protocol message") from None


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one value to canonical ASCII JSON bytes."""
    return canonical_json_dumps(value).encode("ascii")


def canonical_json_loads(value: Union[str, bytes]) -> object:
    """Parse a document only when its complete encoding is canonical JSON."""
    try:
        if type(value) is bytes:
            if len(value) > MAX_MESSAGE_BYTES:
                raise ValueError
            text = value.decode("utf-8")
        elif type(value) is str:
            encoded = value.encode("utf-8")
            if len(encoded) > MAX_MESSAGE_BYTES:
                raise ValueError
            text = value
        else:
            raise TypeError

        parsed = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        _validate_json_value(parsed)
        if canonical_json_dumps(parsed) != text:
            raise ValueError
        return parsed
    except (
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        raise ProtocolFormatError("invalid protocol message") from None


def generate_pairing_key() -> str:
    """Generate a 256-bit canonical URL-safe pairing key."""
    return _encode_token(secrets.token_bytes(_SECRET_BYTES))


def validate_pairing_key(value: object) -> None:
    """Require a canonical 256-bit URL-safe pairing key."""
    _decode_token(value, _SECRET_BYTES)


def generate_nonce() -> str:
    """Generate a 256-bit canonical URL-safe handshake nonce."""
    return _encode_token(secrets.token_bytes(_NONCE_BYTES))


def generate_request_id() -> str:
    """Generate a strong correlation ID accepted by the identifier schema."""
    return "request_" + generate_nonce()


def validate_nonce(value: object) -> None:
    """Require a canonical 256-bit URL-safe handshake nonce."""
    _decode_token(value, _NONCE_BYTES)


def generate_link_id() -> str:
    """Generate an opaque URL-safe identifier for one configured link."""
    return "link_" + _encode_unpadded(secrets.token_bytes(16))


def validate_link_id(value: object) -> None:
    """Require one conservative, log-safe link identifier."""
    _validate_identifier(value)


def generate_destination_id() -> str:
    """Generate an opaque URL-safe identifier for one Domoticz destination."""
    return "domoticz_" + _encode_unpadded(secrets.token_bytes(16))


def validate_destination_id(value: object) -> None:
    """Require one conservative, log-safe destination identifier."""
    _validate_identifier(value)


def validate_protocol_tokens(value: object) -> Tuple[str, ...]:
    """Return one bounded, ordered, duplicate-free WebSocket protocol offer."""
    if type(value) not in {list, tuple}:
        raise ProtocolFormatError("invalid protocol message")
    if not 1 <= len(value) <= MAX_PROTOCOL_TOKENS:
        raise ProtocolFormatError("invalid protocol message")

    result: List[str] = []
    seen = set()
    for token in value:
        if (
            type(token) is not str
            or _WEBSOCKET_TOKEN_RE.fullmatch(token) is None
            or token in seen
        ):
            raise ProtocolFormatError("invalid protocol message")
        seen.add(token)
        result.append(token)
    return tuple(result)


def validate_feature_ids(value: object) -> Tuple[str, ...]:
    """Return one bounded, sorted, duplicate-free optional feature list."""
    if type(value) not in {list, tuple} or len(value) > MAX_FEATURE_IDS:
        raise ProtocolFormatError("invalid protocol message")

    result: List[str] = []
    for feature in value:
        _validate_feature_id(feature)
        result.append(feature)
    if result != sorted(result) or len(result) != len(set(result)):
        raise ProtocolFormatError("invalid protocol message")
    return tuple(result)


def select_websocket_subprotocol(
    client_protocols: object,
    server_protocols: object,
) -> Optional[str]:
    """Select the first client-preferred protocol supported by the server."""
    client = validate_protocol_tokens(client_protocols)
    server = validate_protocol_tokens(server_protocols)
    supported = set(server)
    return next((token for token in client if token in supported), None)


def negotiate_features(
    client_features: object,
    server_features: object,
) -> Tuple[str, ...]:
    """Return the deterministic sorted feature intersection."""
    client = validate_feature_ids(client_features)
    server = validate_feature_ids(server_features)
    return tuple(sorted(set(client).intersection(server)))


def build_hello(
    link_id: str,
    destination_id: str,
    client_nonce: str,
) -> Dict[str, object]:
    """Build the first client handshake message."""
    hello = ClientHello(link_id, destination_id, client_nonce)
    return {
        "version": PROTOCOL_VERSION,
        "type": "hello",
        "link_id": hello.link_id,
        "destination_id": hello.destination_id,
        "client_nonce": hello.client_nonce,
    }


def parse_hello(document: object) -> ClientHello:
    """Parse an exact client hello document."""
    data = _require_message(document, _HELLO_KEYS, "hello")
    return ClientHello(
        link_id=_require_string(data["link_id"]),
        destination_id=_require_string(data["destination_id"]),
        client_nonce=_require_string(data["client_nonce"]),
    )


def make_handshake_context(
    hello: ClientHello,
    server_nonce: str,
) -> HandshakeContext:
    """Combine a validated hello with the server's fresh nonce."""
    if not isinstance(hello, ClientHello):
        raise ProtocolFormatError("invalid protocol message")
    return HandshakeContext(
        link_id=hello.link_id,
        destination_id=hello.destination_id,
        client_nonce=hello.client_nonce,
        server_nonce=server_nonce,
    )


def create_client_proof(
    pairing_key: str,
    context: HandshakeContext,
) -> str:
    """Create the Domoticz proof for one complete handshake transcript."""
    return _create_proof(pairing_key, context, _CLIENT_PROOF_DOMAIN)


def verify_client_proof(
    pairing_key: str,
    context: HandshakeContext,
    proof: object,
) -> None:
    """Verify a Domoticz proof in constant time."""
    _verify_proof(pairing_key, context, proof, _CLIENT_PROOF_DOMAIN)


def create_server_proof(
    pairing_key: str,
    context: HandshakeContext,
) -> str:
    """Create the Home Assistant proof for one complete transcript."""
    return _create_proof(pairing_key, context, _SERVER_PROOF_DOMAIN)


def verify_server_proof(
    pairing_key: str,
    context: HandshakeContext,
    proof: object,
) -> None:
    """Verify a Home Assistant proof in constant time."""
    _verify_proof(pairing_key, context, proof, _SERVER_PROOF_DOMAIN)


def build_challenge(
    pairing_key: str,
    context: HandshakeContext,
) -> Dict[str, object]:
    """Build the server challenge and its transcript-bound proof."""
    _require_context(context)
    return {
        "version": PROTOCOL_VERSION,
        "type": "challenge",
        "server_nonce": context.server_nonce,
        "server_proof": create_server_proof(pairing_key, context),
    }


def accept_challenge(
    pairing_key: str,
    hello: ClientHello,
    document: object,
) -> HandshakeContext:
    """Parse and authenticate a server challenge, returning its context."""
    if not isinstance(hello, ClientHello):
        raise ProtocolFormatError("invalid protocol message")
    data = _require_message(document, _CHALLENGE_KEYS, "challenge")
    server_nonce = _require_string(data["server_nonce"])
    server_proof = _require_string(data["server_proof"])
    context = make_handshake_context(hello, server_nonce)
    verify_server_proof(pairing_key, context, server_proof)
    return context


def build_authenticate(
    pairing_key: str,
    context: HandshakeContext,
) -> Dict[str, object]:
    """Build the client's response to an authenticated challenge."""
    _require_context(context)
    return {
        "version": PROTOCOL_VERSION,
        "type": "authenticate",
        "client_proof": create_client_proof(pairing_key, context),
    }


def verify_authenticate(
    pairing_key: str,
    context: HandshakeContext,
    document: object,
) -> None:
    """Parse and authenticate the client's handshake response."""
    _require_context(context)
    data = _require_message(document, _AUTHENTICATE_KEYS, "authenticate")
    verify_client_proof(pairing_key, context, data["client_proof"])


def derive_session_key(
    pairing_key: str,
    context: HandshakeContext,
) -> bytes:
    """Derive a unique 256-bit session key using an HKDF-style expansion."""
    key = _pairing_key_bytes(pairing_key)
    transcript = _transcript_bytes(context)
    salt = hashlib.sha256(_SESSION_SALT_DOMAIN + transcript).digest()
    extracted = hmac.new(salt, key, hashlib.sha256).digest()
    return hmac.new(
        extracted,
        _SESSION_KEY_DOMAIN + transcript + b"\x01",
        hashlib.sha256,
    ).digest()


def derive_session_id(
    session_key: bytes,
    context: HandshakeContext,
) -> str:
    """Derive an unpredictable identifier bound to one authenticated session."""
    key = _validate_session_key(session_key)
    digest = hmac.new(
        key,
        _SESSION_ID_DOMAIN + _transcript_bytes(context),
        hashlib.sha256,
    ).digest()
    return _encode_token(digest)


def build_ready(
    session_key: bytes,
    context: HandshakeContext,
) -> Dict[str, object]:
    """Build the final server message for an authenticated session."""
    return {
        "version": PROTOCOL_VERSION,
        "type": "ready",
        "session_id": derive_session_id(session_key, context),
    }


def verify_ready(
    session_key: bytes,
    context: HandshakeContext,
    document: object,
) -> str:
    """Verify the final secret-bound session identifier in constant time."""
    data = _require_message(document, _READY_KEYS, "ready")
    received = _token_bytes(data["session_id"])
    expected_id = derive_session_id(session_key, context)
    expected = _token_bytes(expected_id)
    if not hmac.compare_digest(received, expected):
        raise ProtocolAuthenticationError("protocol authentication failed")
    return expected_id


def build_v2_hello(
    link_id: str,
    destination_id: str,
    client_nonce: str,
    *,
    client_protocols: Sequence[str],
    selected_protocol: str,
    client_features: Sequence[str],
) -> Dict[str, object]:
    """Build the authenticated repeat of the HTTP protocol negotiation."""
    hello = V2ClientHello(
        link_id=link_id,
        destination_id=destination_id,
        client_nonce=client_nonce,
        client_protocols=validate_protocol_tokens(client_protocols),
        selected_protocol=selected_protocol,
        client_features=validate_feature_ids(client_features),
    )
    return {
        "version": PROTOCOL_VERSION_V2,
        "type": "hello",
        "link_id": hello.link_id,
        "destination_id": hello.destination_id,
        "client_nonce": hello.client_nonce,
        "client_protocols": list(hello.client_protocols),
        "selected_protocol": hello.selected_protocol,
        "client_features": list(hello.client_features),
    }


def parse_v2_hello(document: object) -> V2ClientHello:
    """Parse one exact v2 client hello."""
    data = _require_versioned_message(
        document,
        _V2_HELLO_KEYS,
        "hello",
        PROTOCOL_VERSION_V2,
    )
    return V2ClientHello(
        link_id=_require_string(data["link_id"]),
        destination_id=_require_string(data["destination_id"]),
        client_nonce=_require_string(data["client_nonce"]),
        client_protocols=_require_wire_protocol_tokens(data["client_protocols"]),
        selected_protocol=_require_string(data["selected_protocol"]),
        client_features=_require_wire_feature_ids(data["client_features"]),
    )


def make_v2_handshake_context(
    hello: V2ClientHello,
    server_nonce: str,
    *,
    server_protocols: Sequence[str],
    server_features: Sequence[str],
) -> V2HandshakeContext:
    """Select and bind one deterministic v2 protocol and feature intersection."""
    if not isinstance(hello, V2ClientHello):
        raise ProtocolFormatError("invalid protocol message")
    protocols = validate_protocol_tokens(server_protocols)
    selected_protocol = select_websocket_subprotocol(
        hello.client_protocols,
        protocols,
    )
    if selected_protocol is None:
        raise ProtocolCompatibilityError("incompatible protocol")
    if selected_protocol != hello.selected_protocol:
        raise ProtocolFormatError("invalid protocol message")

    features = validate_feature_ids(server_features)
    selected_features = negotiate_features(hello.client_features, features)
    return V2HandshakeContext(
        link_id=hello.link_id,
        destination_id=hello.destination_id,
        client_nonce=hello.client_nonce,
        server_nonce=server_nonce,
        client_protocols=hello.client_protocols,
        server_protocols=protocols,
        selected_protocol=selected_protocol,
        client_features=hello.client_features,
        server_features=features,
        selected_features=selected_features,
    )


def create_v2_client_proof(
    pairing_key: str,
    context: V2HandshakeContext,
) -> str:
    """Create the v2 Domoticz proof for the complete negotiation transcript."""
    return _create_v2_proof(pairing_key, context, _V2_CLIENT_PROOF_DOMAIN)


def verify_v2_client_proof(
    pairing_key: str,
    context: V2HandshakeContext,
    proof: object,
) -> None:
    """Verify the v2 Domoticz proof in constant time."""
    _verify_v2_proof(pairing_key, context, proof, _V2_CLIENT_PROOF_DOMAIN)


def create_v2_server_proof(
    pairing_key: str,
    context: V2HandshakeContext,
) -> str:
    """Create the v2 Home Assistant proof for the complete transcript."""
    return _create_v2_proof(pairing_key, context, _V2_SERVER_PROOF_DOMAIN)


def verify_v2_server_proof(
    pairing_key: str,
    context: V2HandshakeContext,
    proof: object,
) -> None:
    """Verify the v2 Home Assistant proof in constant time."""
    _verify_v2_proof(pairing_key, context, proof, _V2_SERVER_PROOF_DOMAIN)


def build_v2_challenge(
    pairing_key: str,
    context: V2HandshakeContext,
) -> Dict[str, object]:
    """Build the exact v2 server selection and transcript-bound proof."""
    validated = _require_v2_context(context)
    return {
        "version": PROTOCOL_VERSION_V2,
        "type": "challenge",
        "server_nonce": validated.server_nonce,
        "server_protocols": list(validated.server_protocols),
        "selected_protocol": validated.selected_protocol,
        "server_features": list(validated.server_features),
        "selected_features": list(validated.selected_features),
        "server_proof": create_v2_server_proof(pairing_key, validated),
    }


def accept_v2_challenge(
    pairing_key: str,
    hello: V2ClientHello,
    document: object,
) -> V2HandshakeContext:
    """Parse and authenticate one deterministic v2 server selection."""
    if not isinstance(hello, V2ClientHello):
        raise ProtocolFormatError("invalid protocol message")
    data = _require_versioned_message(
        document,
        _V2_CHALLENGE_KEYS,
        "challenge",
        PROTOCOL_VERSION_V2,
    )
    context = make_v2_handshake_context(
        hello,
        _require_string(data["server_nonce"]),
        server_protocols=_require_wire_protocol_tokens(data["server_protocols"]),
        server_features=_require_wire_feature_ids(data["server_features"]),
    )
    selected_features = _require_wire_feature_ids(data["selected_features"])
    if (
        data["selected_protocol"] != context.selected_protocol
        or selected_features != context.selected_features
    ):
        raise ProtocolFormatError("invalid protocol message")
    verify_v2_server_proof(pairing_key, context, data["server_proof"])
    return context


def build_v2_authenticate(
    pairing_key: str,
    context: V2HandshakeContext,
) -> Dict[str, object]:
    """Build the v2 client's proof of the authenticated selection."""
    validated = _require_v2_context(context)
    return {
        "version": PROTOCOL_VERSION_V2,
        "type": "authenticate",
        "client_proof": create_v2_client_proof(pairing_key, validated),
    }


def verify_v2_authenticate(
    pairing_key: str,
    context: V2HandshakeContext,
    document: object,
) -> None:
    """Parse and authenticate the v2 client's response."""
    validated = _require_v2_context(context)
    data = _require_versioned_message(
        document,
        _AUTHENTICATE_KEYS,
        "authenticate",
        PROTOCOL_VERSION_V2,
    )
    verify_v2_client_proof(pairing_key, validated, data["client_proof"])


def derive_v2_session_key(
    pairing_key: str,
    context: V2HandshakeContext,
) -> bytes:
    """Derive a v2 session key bound to protocol and feature negotiation."""
    key = _pairing_key_bytes(pairing_key)
    transcript = _v2_transcript_bytes(context)
    salt = hashlib.sha256(_V2_SESSION_SALT_DOMAIN + transcript).digest()
    extracted = hmac.new(salt, key, hashlib.sha256).digest()
    return hmac.new(
        extracted,
        _V2_SESSION_KEY_DOMAIN + transcript + b"\x01",
        hashlib.sha256,
    ).digest()


def derive_v2_session_id(
    session_key: bytes,
    context: V2HandshakeContext,
) -> str:
    """Derive the v2 secret-bound identifier for one negotiated session."""
    key = _validate_session_key(session_key)
    digest = hmac.new(
        key,
        _V2_SESSION_ID_DOMAIN + _v2_transcript_bytes(context),
        hashlib.sha256,
    ).digest()
    return _encode_token(digest)


def build_v2_ready(
    session_key: bytes,
    context: V2HandshakeContext,
) -> Dict[str, object]:
    """Build the final v2 server handshake message."""
    return {
        "version": PROTOCOL_VERSION_V2,
        "type": "ready",
        "session_id": derive_v2_session_id(session_key, context),
    }


def verify_v2_ready(
    session_key: bytes,
    context: V2HandshakeContext,
    document: object,
) -> str:
    """Verify the final v2 secret-bound session identifier."""
    data = _require_versioned_message(
        document,
        _READY_KEYS,
        "ready",
        PROTOCOL_VERSION_V2,
    )
    received = _token_bytes(data["session_id"])
    expected_id = derive_v2_session_id(session_key, context)
    expected = _token_bytes(expected_id)
    if not hmac.compare_digest(received, expected):
        raise ProtocolAuthenticationError("protocol authentication failed")
    return expected_id


def sign_envelope(
    session_key: bytes,
    *,
    protocol_version: int,
    direction: str,
    session_id: str,
    sequence: int,
    payload: object,
) -> Dict[str, object]:
    """Build and sign one directional application envelope."""
    key = _validate_session_key(session_key)
    domain = _envelope_domain(protocol_version)
    _validate_direction(direction)
    _token_bytes(session_id)
    _validate_positive_sequence(sequence)
    normalized_payload = _normalize_payload(payload)
    unsigned = _unsigned_envelope(
        protocol_version,
        direction,
        session_id,
        sequence,
        normalized_payload,
    )
    signature = hmac.new(
        key,
        domain + canonical_json_bytes(unsigned),
        hashlib.sha256,
    ).digest()
    envelope = dict(unsigned)
    envelope["signature"] = _encode_token(signature)
    return envelope


def verify_envelope(
    session_key: bytes,
    document: object,
    *,
    protocol_version: int,
    expected_direction: str,
    expected_session_id: str,
    last_sequence: int,
) -> VerifiedEnvelope:
    """Authenticate one envelope and require the exact next sequence number."""
    key = _validate_session_key(session_key)
    domain = _envelope_domain(protocol_version)
    _validate_direction(expected_direction)
    expected_session = _token_bytes(expected_session_id)
    _validate_last_sequence(last_sequence)

    data = _require_versioned_message(
        document,
        _ENVELOPE_KEYS,
        "message",
        protocol_version,
    )
    direction = _require_string(data["direction"])
    _validate_direction(direction)
    session_id = _require_string(data["session_id"])
    received_session = _token_bytes(session_id)
    sequence = data["sequence"]
    _validate_positive_sequence(sequence)
    payload = _normalize_payload(data["payload"])
    signature = _token_bytes(data["signature"])

    unsigned = _unsigned_envelope(
        protocol_version,
        direction,
        session_id,
        sequence,
        payload,
    )
    expected_signature = hmac.new(
        key,
        domain + canonical_json_bytes(unsigned),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ProtocolAuthenticationError("protocol authentication failed")
    if direction != expected_direction or not hmac.compare_digest(
        received_session,
        expected_session,
    ):
        raise ProtocolAuthenticationError("protocol authentication failed")
    if sequence != last_sequence + 1:
        raise ProtocolSequenceError("invalid protocol sequence")

    return VerifiedEnvelope(
        session_id=session_id,
        direction=direction,
        sequence=sequence,
        payload=payload,
    )


def build_application_ready(
    selection: ProtocolSelection,
) -> Dict[str, object]:
    """Build the feature-independent v2 application lifecycle message."""
    _require_v2_selection(selection)
    return {"schema": 1, "type": "application_ready"}


def parse_application_ready(
    selection: ProtocolSelection,
    document: object,
) -> None:
    """Parse the exact feature-independent v2 lifecycle message."""
    _require_v2_selection(selection)
    _require_application_message(
        _normalize_payload(document),
        _APPLICATION_READY_KEYS,
        "application_ready",
    )


def build_inventory_request(
    selection: ProtocolSelection,
    request_id: str,
) -> Dict[str, object]:
    """Build one feature-gated request for a complete Domoticz inventory."""
    _require_inventory_selection(selection)
    _validate_request_id(request_id)
    return _normalize_inventory_payload(
        {
            "schema": 1,
            "type": "inventory_request",
            "request_id": request_id,
        }
    )


def parse_inventory_request(
    selection: ProtocolSelection,
    document: object,
) -> str:
    """Parse one exact inventory request and return its correlation ID."""
    _require_inventory_selection(selection)
    try:
        data = _require_application_message(
            _normalize_inventory_payload(document),
            _INVENTORY_REQUEST_KEYS,
            "inventory_request",
        )
        request_id = _require_string(data["request_id"])
        _validate_request_id(request_id)
        return request_id
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProtocolFormatError("invalid protocol message") from None


def build_inventory_result(
    selection: ProtocolSelection,
    result: InventoryResult,
) -> Dict[str, object]:
    """Build one strict, bounded page of a Domoticz inventory result."""
    _require_inventory_selection(selection)
    if not isinstance(result, InventoryResult):
        raise ProtocolFormatError("invalid protocol message")
    return _normalize_inventory_payload(_inventory_result_to_dict(result))


def parse_inventory_result(
    selection: ProtocolSelection,
    document: object,
) -> InventoryResult:
    """Parse one exact, bounded Domoticz inventory result page."""
    _require_inventory_selection(selection)
    try:
        data = _require_application_message(
            _normalize_inventory_payload(document),
            _INVENTORY_RESULT_KEYS,
            "inventory_result",
        )
        targets = data["targets"]
        if type(targets) is not list:
            raise ProtocolFormatError("invalid protocol message")
        return InventoryResult(
            request_id=_require_string(data["request_id"]),
            status=InventoryResultStatus(data["status"]),
            page=data["page"],
            complete=data["complete"],
            targets=tuple(_inventory_target_from_dict(target) for target in targets),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProtocolFormatError("invalid protocol message") from None


def assemble_inventory_results(
    selection: ProtocolSelection,
    request_id: str,
    pages: Iterable[InventoryResult],
) -> Tuple[InventoryTarget, ...]:
    """Validate and assemble one complete inventory without side effects."""
    validated_selection = _require_inventory_selection(selection)
    _validate_request_id(request_id)
    try:
        iterator = iter(pages)
    except TypeError:
        raise ProtocolFormatError("invalid protocol message") from None

    assembled: List[InventoryTarget] = []
    page_count = 0
    unit_count = 0
    terminal_seen = False
    previous_target_id: Optional[str] = None

    for result in iterator:
        page_count += 1
        if page_count > MAX_INVENTORY_PAGES:
            raise ProtocolFormatError("invalid protocol message")
        if not isinstance(result, InventoryResult):
            raise ProtocolFormatError("invalid protocol message")
        if (
            terminal_seen
            or result.request_id != request_id
            or result.page != page_count
        ):
            raise ProtocolFormatError("invalid protocol message")

        # Directly constructed pages receive the same canonical byte validation
        # as pages that crossed the wire.
        build_inventory_result(validated_selection, result)

        if result.status is InventoryResultStatus.REJECTED:
            raise ProtocolCompatibilityError("inventory rejected")

        for target in result.targets:
            if (
                previous_target_id is not None
                and target.target_id <= previous_target_id
            ):
                raise ProtocolFormatError("invalid protocol message")
            previous_target_id = target.target_id
            assembled.append(target)
            unit_count += len(target.units)
            if (
                len(assembled) > MAX_INVENTORY_TARGETS
                or unit_count > MAX_INVENTORY_UNITS
            ):
                raise ProtocolFormatError("invalid protocol message")

        terminal_seen = result.complete

    if page_count == 0 or not terminal_seen:
        raise ProtocolFormatError("invalid protocol message")
    return tuple(assembled)


def build_apply(
    selection: ProtocolSelection,
    request_id: str,
    action: ReconciliationAction,
) -> Dict[str, object]:
    """Build one strict Home Assistant-to-Domoticz application request."""
    _require_numeric_export_selection(selection)
    request = ApplyRequest(request_id=request_id, action=action)
    if request.action.capability.kind is not CapabilityKind.NUMERIC:
        raise ProtocolFormatError("invalid protocol message")
    return _normalize_payload(
        {
            "schema": 1,
            "type": "apply",
            "request_id": request.request_id,
            "action": _action_to_dict(request.action),
        }
    )


def parse_apply(
    selection: ProtocolSelection,
    document: object,
) -> ApplyRequest:
    """Parse one exact application request into the neutral action model."""
    _require_numeric_export_selection(selection)
    try:
        data = _require_application_message(
            _normalize_payload(document),
            _APPLY_KEYS,
            "apply",
        )
        request = ApplyRequest(
            request_id=_require_string(data["request_id"]),
            action=_action_from_dict(data["action"]),
        )
        if request.action.capability.kind is not CapabilityKind.NUMERIC:
            raise ProtocolFormatError("invalid protocol message")
        return request
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProtocolFormatError("invalid protocol message") from None


def build_apply_result(
    selection: ProtocolSelection,
    request_id: str,
    status: ApplyResultStatus,
    target_id: Optional[str],
    source: Optional[SourceIdentity],
) -> Dict[str, object]:
    """Build one strict Domoticz-to-Home Assistant action result."""
    _require_numeric_export_selection(selection)
    result = ApplyResult(
        request_id=request_id,
        status=status,
        target_id=target_id,
        source=source,
    )
    return _normalize_payload(
        {
            "schema": 1,
            "type": "apply_result",
            "request_id": result.request_id,
            "status": result.status.value,
            "target_id": result.target_id,
            "source": (
                _source_to_dict(result.source) if result.source is not None else None
            ),
        }
    )


def parse_apply_result(
    selection: ProtocolSelection,
    document: object,
) -> ApplyResult:
    """Parse one exact action result without accepting remote error details."""
    _require_numeric_export_selection(selection)
    try:
        data = _require_application_message(
            _normalize_payload(document),
            _APPLY_RESULT_KEYS,
            "apply_result",
        )
        source_data = data["source"]
        return ApplyResult(
            request_id=_require_string(data["request_id"]),
            status=ApplyResultStatus(data["status"]),
            target_id=data["target_id"],
            source=(
                _source_from_dict(source_data) if source_data is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProtocolFormatError("invalid protocol message") from None


def build_binary_apply(
    selection: ProtocolSelection,
    request_id: str,
    action: ReconciliationAction,
) -> Dict[str, object]:
    """Build one strict binary Home Assistant-to-Domoticz request."""
    _require_binary_export_selection(selection)
    request = ApplyRequest(request_id=request_id, action=action)
    if request.action.capability.kind is not CapabilityKind.BINARY:
        raise ProtocolFormatError("invalid protocol message")
    return _normalize_payload(
        {
            "schema": 1,
            "type": "binary_apply",
            "request_id": request.request_id,
            "action": _action_to_dict(request.action),
        }
    )


def parse_binary_apply(
    selection: ProtocolSelection,
    document: object,
) -> ApplyRequest:
    """Parse one exact binary request into the neutral action model."""
    _require_binary_export_selection(selection)
    try:
        data = _require_application_message(
            _normalize_payload(document),
            _APPLY_KEYS,
            "binary_apply",
        )
        request = ApplyRequest(
            request_id=_require_string(data["request_id"]),
            action=_action_from_dict(data["action"]),
        )
        if request.action.capability.kind is not CapabilityKind.BINARY:
            raise ProtocolFormatError("invalid protocol message")
        return request
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProtocolFormatError("invalid protocol message") from None


def build_binary_apply_result(
    selection: ProtocolSelection,
    request_id: str,
    status: ApplyResultStatus,
    target_id: Optional[str],
    source: Optional[SourceIdentity],
) -> Dict[str, object]:
    """Build one strict binary Domoticz-to-Home Assistant action result."""
    _require_binary_export_selection(selection)
    result = ApplyResult(
        request_id=request_id,
        status=status,
        target_id=target_id,
        source=source,
    )
    return _normalize_payload(
        {
            "schema": 1,
            "type": "binary_apply_result",
            "request_id": result.request_id,
            "status": result.status.value,
            "target_id": result.target_id,
            "source": (
                _source_to_dict(result.source) if result.source is not None else None
            ),
        }
    )


def parse_binary_apply_result(
    selection: ProtocolSelection,
    document: object,
) -> ApplyResult:
    """Parse one exact binary result without accepting remote error details."""
    _require_binary_export_selection(selection)
    try:
        data = _require_application_message(
            _normalize_payload(document),
            _APPLY_RESULT_KEYS,
            "binary_apply_result",
        )
        source_data = data["source"]
        return ApplyResult(
            request_id=_require_string(data["request_id"]),
            status=ApplyResultStatus(data["status"]),
            target_id=data["target_id"],
            source=(
                _source_from_dict(source_data) if source_data is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ProtocolFormatError("invalid protocol message") from None


def _object_without_duplicate_keys(
    pairs: List[Tuple[str, object]],
) -> Dict[str, object]:
    """Reject duplicate JSON object fields instead of silently replacing them."""
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject non-standard NaN and infinity literals."""
    raise ValueError


def _validate_json_value(value: object) -> None:
    """Validate the deterministic, interoperable subset used on the wire."""
    pending: List[Tuple[object, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if not -MAX_SAFE_INTEGER <= current <= MAX_SAFE_INTEGER:
                raise ValueError
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise ValueError
            continue
        if type(current) is str:
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise ValueError
            continue
        if type(current) is list:
            if depth >= MAX_JSON_DEPTH:
                raise ValueError
            pending.extend((item, depth + 1) for item in current)
            continue
        if type(current) is dict:
            if depth >= MAX_JSON_DEPTH:
                raise ValueError
            for key, item in current.items():
                if type(key) is not str:
                    raise TypeError
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
            continue
        raise TypeError


def _validate_identifier(value: object) -> None:
    """Validate an identifier without reflecting it into an exception."""
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ProtocolFormatError("invalid protocol message")


def _validate_feature_id(value: object) -> None:
    """Require one conservative feature identifier safe for diagnostics."""
    _validate_identifier(value)


def _validate_request_id(value: object) -> None:
    """Require a bounded, log-safe request correlation identifier."""
    _validate_identifier(value)


def _validate_bounded_integer(value: object, minimum: int, maximum: int) -> None:
    """Require one exact integer inside an inclusive wire-safe range."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolFormatError("invalid protocol message")


def _validate_strict_bool(value: object) -> None:
    """Require a JSON boolean without accepting integer zero or one."""
    if type(value) is not bool:
        raise ProtocolFormatError("invalid protocol message")


def _validate_inventory_string(value: object, maximum_bytes: int) -> None:
    """Require one valid Unicode string inside its UTF-8 byte bound."""
    try:
        if type(value) is not str or len(value.encode("utf-8")) > maximum_bytes:
            raise ValueError
        _validate_json_value(value)
    except (UnicodeError, TypeError, ValueError, OverflowError, RecursionError):
        raise ProtocolFormatError("invalid protocol message") from None


def _validate_inventory_target_id(value: object) -> None:
    """Require one bounded, nonempty, whitespace-stable target identity."""
    _validate_inventory_string(value, MAX_INVENTORY_TARGET_ID_BYTES)
    if not value or value != value.strip():
        raise ProtocolFormatError("invalid protocol message")


def _validate_target_id(value: object) -> None:
    """Apply the target-neutral opaque identifier rules."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProtocolFormatError("invalid protocol message")


def _validate_direction(value: object) -> None:
    """Require one of the two role-specific wire directions."""
    if type(value) is not str or value not in _DIRECTIONS:
        raise ProtocolFormatError("invalid protocol message")


def _validate_positive_sequence(value: object) -> None:
    """Require a positive interoperable sequence integer."""
    if type(value) is not int or not 1 <= value <= MAX_SEQUENCE:
        raise ProtocolFormatError("invalid protocol message")


def _validate_last_sequence(value: object) -> None:
    """Require a valid local sequence checkpoint."""
    if type(value) is not int or not 0 <= value < MAX_SEQUENCE:
        raise ProtocolFormatError("invalid protocol message")


def _require_string(value: object) -> str:
    """Return a strict string field or one generic format error."""
    if type(value) is not str:
        raise ProtocolFormatError("invalid protocol message")
    return value


def _require_wire_protocol_tokens(value: object) -> Tuple[str, ...]:
    """Parse a JSON array containing an ordered WebSocket protocol offer."""
    if type(value) is not list:
        raise ProtocolFormatError("invalid protocol message")
    return validate_protocol_tokens(value)


def _require_wire_feature_ids(value: object) -> Tuple[str, ...]:
    """Parse a JSON array containing sorted optional feature identifiers."""
    if type(value) is not list:
        raise ProtocolFormatError("invalid protocol message")
    return validate_feature_ids(value)


def _require_message(
    document: object,
    expected_keys: set,
    expected_type: str,
) -> Dict[str, object]:
    """Require one exact v1 message object and discriminator."""
    if type(document) is not dict or set(document) != expected_keys:
        raise ProtocolFormatError("invalid protocol message")
    if type(document["version"]) is not int or (
        document["version"] != PROTOCOL_VERSION
    ):
        raise ProtocolFormatError("invalid protocol message")
    if type(document["type"]) is not str or document["type"] != expected_type:
        raise ProtocolFormatError("invalid protocol message")
    return document


def _require_versioned_message(
    document: object,
    expected_keys: set,
    expected_type: str,
    expected_version: int,
) -> Dict[str, object]:
    """Require exact keys and one explicitly selected supported wire version."""
    if expected_version not in {PROTOCOL_VERSION, PROTOCOL_VERSION_V2}:
        raise ProtocolFormatError("invalid protocol message")
    if type(document) is not dict or set(document) != expected_keys:
        raise ProtocolFormatError("invalid protocol message")
    if type(document["version"]) is not int or document["version"] != expected_version:
        raise ProtocolFormatError("invalid protocol message")
    if type(document["type"]) is not str or document["type"] != expected_type:
        raise ProtocolFormatError("invalid protocol message")
    return document


def _require_application_message(
    document: object,
    expected_keys: set,
    expected_type: str,
) -> Dict[str, object]:
    """Require one exact application payload and its discriminator."""
    if type(document) is not dict or set(document) != expected_keys:
        raise ProtocolFormatError("invalid protocol message")
    if type(document["schema"]) is not int or document["schema"] != 1:
        raise ProtocolFormatError("invalid protocol message")
    if type(document["type"]) is not str or document["type"] != expected_type:
        raise ProtocolFormatError("invalid protocol message")
    return document


def _require_context(context: object) -> HandshakeContext:
    """Require the validated context type."""
    if not isinstance(context, HandshakeContext):
        raise ProtocolFormatError("invalid protocol message")
    return context


def _require_v2_context(context: object) -> V2HandshakeContext:
    """Require one validated v2 handshake context."""
    if not isinstance(context, V2HandshakeContext):
        raise ProtocolFormatError("invalid protocol message")
    return context


def _require_v2_selection(selection: object) -> ProtocolSelection:
    """Require an authenticated v2 protocol selection."""
    if not isinstance(selection, ProtocolSelection):
        raise ProtocolFormatError("invalid protocol message")
    if (
        selection.version != PROTOCOL_VERSION_V2
        or selection.websocket_subprotocol != WEBSOCKET_SUBPROTOCOL_V2
    ):
        raise ProtocolFormatError("invalid protocol message")
    return selection


def _require_numeric_export_selection(
    selection: object,
) -> ProtocolSelection:
    """Require the negotiated Home Assistant numeric export behavior."""
    validated = _require_v2_selection(selection)
    if not validated.supports(FEATURE_HA_EXPORT_NUMERIC_V1):
        raise ProtocolCompatibilityError("incompatible protocol")
    return validated


def _require_binary_export_selection(
    selection: object,
) -> ProtocolSelection:
    """Require the negotiated Home Assistant binary export behavior."""
    validated = _require_v2_selection(selection)
    if not validated.supports(FEATURE_HA_EXPORT_BINARY_V1):
        raise ProtocolCompatibilityError("incompatible protocol")
    return validated


def _require_inventory_selection(
    selection: object,
) -> ProtocolSelection:
    """Require the negotiated authenticated Domoticz inventory behavior."""
    validated = _require_v2_selection(selection)
    if not validated.supports(FEATURE_DOMOTICZ_INVENTORY_V1):
        raise ProtocolCompatibilityError("incompatible protocol")
    return validated


def _transcript_bytes(context: HandshakeContext) -> bytes:
    """Return the canonical public handshake transcript."""
    validated = _require_context(context)
    return canonical_json_bytes(
        {
            "version": PROTOCOL_VERSION,
            "link_id": validated.link_id,
            "destination_id": validated.destination_id,
            "client_nonce": validated.client_nonce,
            "server_nonce": validated.server_nonce,
        }
    )


def _v2_transcript_bytes(context: V2HandshakeContext) -> bytes:
    """Return the canonical complete v2 negotiation transcript."""
    validated = _require_v2_context(context)
    return canonical_json_bytes(
        {
            "version": PROTOCOL_VERSION_V2,
            "link_id": validated.link_id,
            "destination_id": validated.destination_id,
            "client_nonce": validated.client_nonce,
            "server_nonce": validated.server_nonce,
            "client_protocols": list(validated.client_protocols),
            "server_protocols": list(validated.server_protocols),
            "selected_protocol": validated.selected_protocol,
            "client_features": list(validated.client_features),
            "server_features": list(validated.server_features),
            "selected_features": list(validated.selected_features),
        }
    )


def _create_proof(
    pairing_key: str,
    context: HandshakeContext,
    domain: bytes,
) -> str:
    """Create one role-separated transcript proof."""
    key = _pairing_key_bytes(pairing_key)
    proof = hmac.new(
        key,
        domain + _transcript_bytes(context),
        hashlib.sha256,
    ).digest()
    return _encode_token(proof)


def _verify_proof(
    pairing_key: str,
    context: HandshakeContext,
    proof: object,
    domain: bytes,
) -> None:
    """Authenticate one role-separated proof in constant time."""
    received = _token_bytes(proof)
    expected = _token_bytes(_create_proof(pairing_key, context, domain))
    if not hmac.compare_digest(received, expected):
        raise ProtocolAuthenticationError("protocol authentication failed")


def _create_v2_proof(
    pairing_key: str,
    context: V2HandshakeContext,
    domain: bytes,
) -> str:
    """Create one role-separated v2 negotiation proof."""
    key = _pairing_key_bytes(pairing_key)
    proof = hmac.new(
        key,
        domain + _v2_transcript_bytes(context),
        hashlib.sha256,
    ).digest()
    return _encode_token(proof)


def _verify_v2_proof(
    pairing_key: str,
    context: V2HandshakeContext,
    proof: object,
    domain: bytes,
) -> None:
    """Authenticate one v2 proof in constant time."""
    received = _token_bytes(proof)
    expected = _token_bytes(_create_v2_proof(pairing_key, context, domain))
    if not hmac.compare_digest(received, expected):
        raise ProtocolAuthenticationError("protocol authentication failed")


def _envelope_domain(protocol_version: object) -> bytes:
    """Return the role-independent MAC domain for one supported wire version."""
    if protocol_version == PROTOCOL_VERSION and type(protocol_version) is int:
        return _ENVELOPE_DOMAIN
    if protocol_version == PROTOCOL_VERSION_V2 and type(protocol_version) is int:
        return _V2_ENVELOPE_DOMAIN
    raise ProtocolFormatError("invalid protocol message")


def _pairing_key_bytes(pairing_key: object) -> bytes:
    """Decode a validated key without exposing it in an error."""
    return _decode_token(pairing_key, _SECRET_BYTES)


def _token_bytes(value: object) -> bytes:
    """Decode one canonical 256-bit proof, session ID, or signature."""
    return _decode_token(value, _TOKEN_BYTES)


def _decode_token(value: object, expected_bytes: int) -> bytes:
    """Decode canonical unpadded base64url into an exact byte length."""
    try:
        if type(value) is not str or _TOKEN_RE.fullmatch(value) is None:
            raise ValueError
        decoded = base64.b64decode(
            value.encode("ascii") + b"=",
            altchars=b"-_",
            validate=True,
        )
        if len(decoded) != expected_bytes or _encode_unpadded(decoded) != value:
            raise ValueError
        return decoded
    except _TOKEN_DECODE_ERRORS:
        raise ProtocolFormatError("invalid protocol message") from None


def _encode_token(value: bytes) -> str:
    """Encode one 256-bit protocol token without base64 padding."""
    if type(value) is not bytes or len(value) != _TOKEN_BYTES:
        raise ProtocolFormatError("invalid protocol message")
    return _encode_unpadded(value)


def _encode_unpadded(value: bytes) -> str:
    """Encode bytes as canonical URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _validate_session_key(session_key: object) -> bytes:
    """Require an exact 256-bit derived key without reflecting it."""
    if type(session_key) is not bytes or len(session_key) != _TOKEN_BYTES:
        raise ProtocolFormatError("invalid protocol message")
    return session_key


def _normalize_payload(payload: object) -> Dict[str, object]:
    """Validate and defensively copy one JSON object payload."""
    if type(payload) is not dict:
        raise ProtocolFormatError("invalid protocol message")
    normalized = canonical_json_loads(canonical_json_dumps(payload))
    if type(normalized) is not dict:
        raise ProtocolFormatError("invalid protocol message")
    return normalized


def _normalize_inventory_payload(payload: object) -> Dict[str, object]:
    """Normalize one inventory payload inside its reserved envelope budget."""
    normalized = _normalize_payload(payload)
    if len(canonical_json_bytes(normalized)) > MAX_INVENTORY_PAYLOAD_BYTES:
        raise ProtocolFormatError("invalid protocol message")
    return normalized


def _inventory_unit_to_dict(unit: InventoryUnit) -> Dict[str, object]:
    """Serialize one complete, bounded inventory unit."""
    return {
        "unit": unit.unit,
        "name": unit.name,
        "type": unit.type,
        "subtype": unit.subtype,
        "switch_type": unit.switch_type,
        "used": unit.used,
        "n_value": unit.n_value,
        "s_value": unit.s_value,
        "custom_option": unit.custom_option,
        "has_other_options": unit.has_other_options,
    }


def _inventory_unit_from_dict(document: object) -> InventoryUnit:
    """Parse one exact inventory unit using strict scalar types."""
    _require_exact_object(document, _INVENTORY_UNIT_KEYS)
    return InventoryUnit(
        unit=document["unit"],
        name=document["name"],
        type=document["type"],
        subtype=document["subtype"],
        switch_type=document["switch_type"],
        used=document["used"],
        n_value=document["n_value"],
        s_value=document["s_value"],
        custom_option=document["custom_option"],
        has_other_options=document["has_other_options"],
    )


def _inventory_target_to_dict(target: InventoryTarget) -> Dict[str, object]:
    """Serialize one parent target and all of its ordered units."""
    return {
        "target_id": target.target_id,
        "timed_out": target.timed_out,
        "units": [_inventory_unit_to_dict(unit) for unit in target.units],
    }


def _inventory_target_from_dict(document: object) -> InventoryTarget:
    """Parse one exact parent target without dropping empty containers."""
    _require_exact_object(document, _INVENTORY_TARGET_KEYS)
    units = document["units"]
    if type(units) is not list:
        raise ProtocolFormatError("invalid protocol message")
    return InventoryTarget(
        target_id=document["target_id"],
        timed_out=document["timed_out"],
        units=tuple(_inventory_unit_from_dict(unit) for unit in units),
    )


def _inventory_result_to_dict(result: InventoryResult) -> Dict[str, object]:
    """Serialize one exact inventory result page."""
    return {
        "schema": 1,
        "type": "inventory_result",
        "request_id": result.request_id,
        "status": result.status.value,
        "page": result.page,
        "complete": result.complete,
        "targets": [
            _inventory_target_to_dict(target) for target in result.targets
        ],
    }


def _source_to_dict(source: SourceIdentity) -> Dict[str, object]:
    """Serialize one complete source identity."""
    return {
        "system": source.system,
        "instance_id": source.instance_id,
        "object_id": source.object_id,
        "capability_id": source.capability_id,
    }


def _source_from_dict(document: object) -> SourceIdentity:
    """Parse one exact source identity using its neutral model rules."""
    _require_exact_object(document, _SOURCE_KEYS)
    return SourceIdentity(
        system=document["system"],
        instance_id=document["instance_id"],
        object_id=document["object_id"],
        capability_id=document["capability_id"],
    )


def _capability_to_dict(capability: Capability) -> Dict[str, object]:
    """Serialize every field that defines one capability snapshot."""
    return {
        "source": _source_to_dict(capability.source),
        "kind": capability.kind.value,
        "name": capability.name,
        "value": capability.value,
        "availability": capability.availability.value,
        "semantic": capability.semantic,
        "unit": capability.unit,
        "state_class": capability.state_class,
    }


def _capability_from_dict(document: object) -> Capability:
    """Parse one exact capability using the complete neutral semantics."""
    _require_exact_object(document, _CAPABILITY_KEYS)
    return Capability(
        source=_source_from_dict(document["source"]),
        kind=CapabilityKind(document["kind"]),
        name=document["name"],
        value=document["value"],
        availability=Availability(document["availability"]),
        semantic=document["semantic"],
        unit=document["unit"],
        state_class=document["state_class"],
    )


def _action_to_dict(action: ReconciliationAction) -> Dict[str, object]:
    """Serialize every field that defines one reconciliation action."""
    return {
        "kind": action.kind.value,
        "capability": _capability_to_dict(action.capability),
        "target_id": action.target_id,
        "stale": action.stale,
    }


def _action_from_dict(document: object) -> ReconciliationAction:
    """Parse one exact action using the complete neutral model semantics."""
    _require_exact_object(document, _ACTION_KEYS)
    return ReconciliationAction(
        kind=ReconciliationActionKind(document["kind"]),
        capability=_capability_from_dict(document["capability"]),
        target_id=document["target_id"],
        stale=document["stale"],
    )


def _require_exact_object(document: object, expected_keys: set) -> None:
    """Reject missing, extra, and non-object nested application fields."""
    if type(document) is not dict or set(document) != expected_keys:
        raise ProtocolFormatError("invalid protocol message")


def _unsigned_envelope(
    protocol_version: int,
    direction: str,
    session_id: str,
    sequence: int,
    payload: Dict[str, object],
) -> Dict[str, object]:
    """Return the exact application fields covered by the envelope MAC."""
    return {
        "version": protocol_version,
        "type": "message",
        "session_id": session_id,
        "direction": direction,
        "sequence": sequence,
        "payload": payload,
    }


__all__ = [
    "DIRECTION_DOMOTICZ_TO_HA",
    "DIRECTION_HA_TO_DOMOTICZ",
    "FEATURE_DOMOTICZ_INVENTORY_V1",
    "FEATURE_HA_EXPORT_BINARY_V1",
    "FEATURE_HA_EXPORT_NUMERIC_V1",
    "INVENTORY_TIMEOUT_SECONDS",
    "MAX_FEATURE_IDS",
    "MAX_INVENTORY_NAME_BYTES",
    "MAX_INVENTORY_OPTION_BYTES",
    "MAX_INVENTORY_PAGES",
    "MAX_INVENTORY_PAYLOAD_BYTES",
    "MAX_INVENTORY_S_VALUE_BYTES",
    "MAX_INVENTORY_TARGET_ID_BYTES",
    "MAX_INVENTORY_TARGETS",
    "MAX_INVENTORY_TARGETS_PER_PAGE",
    "MAX_INVENTORY_UNITS",
    "MAX_MESSAGE_BYTES",
    "MAX_PROTOCOL_TOKENS",
    "MAX_SEQUENCE",
    "NONCE_BITS",
    "PAIRING_KEY_BITS",
    "PROTOCOL_VERSION",
    "PROTOCOL_VERSION_V1",
    "PROTOCOL_VERSION_V2",
    "SUPPORTED_V2_FEATURES",
    "SUPPORTED_WEBSOCKET_SUBPROTOCOLS",
    "WEBSOCKET_SUBPROTOCOL_V2",
    "ApplyRequest",
    "ApplyResult",
    "ApplyResultStatus",
    "ClientHello",
    "HandshakeContext",
    "InventoryResult",
    "InventoryResultStatus",
    "InventoryTarget",
    "InventoryUnit",
    "ProtocolAuthenticationError",
    "ProtocolCompatibilityError",
    "ProtocolError",
    "ProtocolFormatError",
    "ProtocolSelection",
    "ProtocolSequenceError",
    "V2ClientHello",
    "V2HandshakeContext",
    "VerifiedEnvelope",
    "accept_challenge",
    "accept_v2_challenge",
    "assemble_inventory_results",
    "build_application_ready",
    "build_apply",
    "build_apply_result",
    "build_binary_apply",
    "build_binary_apply_result",
    "build_authenticate",
    "build_challenge",
    "build_hello",
    "build_inventory_request",
    "build_inventory_result",
    "build_ready",
    "build_v2_authenticate",
    "build_v2_challenge",
    "build_v2_hello",
    "build_v2_ready",
    "canonical_json_bytes",
    "canonical_json_dumps",
    "canonical_json_loads",
    "create_client_proof",
    "create_server_proof",
    "create_v2_client_proof",
    "create_v2_server_proof",
    "derive_domoticz_target_id",
    "derive_session_id",
    "derive_session_key",
    "derive_v2_session_id",
    "derive_v2_session_key",
    "generate_destination_id",
    "generate_link_id",
    "generate_nonce",
    "generate_pairing_key",
    "generate_request_id",
    "make_handshake_context",
    "make_v2_handshake_context",
    "negotiate_features",
    "parse_apply",
    "parse_apply_result",
    "parse_application_ready",
    "parse_binary_apply",
    "parse_binary_apply_result",
    "parse_hello",
    "parse_inventory_request",
    "parse_inventory_result",
    "parse_v2_hello",
    "select_websocket_subprotocol",
    "sign_envelope",
    "validate_destination_id",
    "validate_feature_ids",
    "validate_link_id",
    "validate_nonce",
    "validate_pairing_key",
    "validate_protocol_tokens",
    "verify_authenticate",
    "verify_client_proof",
    "verify_envelope",
    "verify_ready",
    "verify_server_proof",
    "verify_v2_authenticate",
    "verify_v2_client_proof",
    "verify_v2_ready",
    "verify_v2_server_proof",
]
