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
from typing import Dict, List, Tuple, Union

PROTOCOL_VERSION = 1

DIRECTION_DOMOTICZ_TO_HA = "domoticz_to_home_assistant"
DIRECTION_HA_TO_DOMOTICZ = "home_assistant_to_domoticz"

PAIRING_KEY_BITS = 256
NONCE_BITS = 256
MAX_MESSAGE_BYTES = 262_144
MAX_JSON_DEPTH = 32
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_SEQUENCE = MAX_SAFE_INTEGER

_SECRET_BYTES = PAIRING_KEY_BITS // 8
_NONCE_BYTES = NONCE_BITS // 8
_TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TOKEN_DECODE_ERRORS = (UnicodeError, TypeError, ValueError)
_DIRECTIONS = {
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
}

_HELLO_KEYS = {"version", "type", "link_id", "destination_id", "client_nonce"}
_CHALLENGE_KEYS = {"version", "type", "server_nonce", "server_proof"}
_AUTHENTICATE_KEYS = {"version", "type", "client_proof"}
_READY_KEYS = {"version", "type", "session_id"}
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


class ProtocolError(ValueError):
    """Base class for safe protocol failures."""


class ProtocolFormatError(ProtocolError):
    """A protocol document or value does not match the v1 format."""


class ProtocolAuthenticationError(ProtocolError):
    """A proof, signature, or authenticated session value is invalid."""


class ProtocolSequenceError(ProtocolError):
    """An authenticated envelope is replayed, missing, or out of order."""


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
class VerifiedEnvelope:
    """One authenticated, in-order application payload."""

    session_id: str
    direction: str
    sequence: int
    payload: Dict[str, object]


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
    """Parse a document only when its complete encoding is canonical v1 JSON."""
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


def sign_envelope(
    session_key: bytes,
    *,
    direction: str,
    session_id: str,
    sequence: int,
    payload: object,
) -> Dict[str, object]:
    """Build and sign one directional application envelope."""
    key = _validate_session_key(session_key)
    _validate_direction(direction)
    _token_bytes(session_id)
    _validate_positive_sequence(sequence)
    normalized_payload = _normalize_payload(payload)
    unsigned = _unsigned_envelope(
        direction,
        session_id,
        sequence,
        normalized_payload,
    )
    signature = hmac.new(
        key,
        _ENVELOPE_DOMAIN + canonical_json_bytes(unsigned),
        hashlib.sha256,
    ).digest()
    envelope = dict(unsigned)
    envelope["signature"] = _encode_token(signature)
    return envelope


def verify_envelope(
    session_key: bytes,
    document: object,
    *,
    expected_direction: str,
    expected_session_id: str,
    last_sequence: int,
) -> VerifiedEnvelope:
    """Authenticate one envelope and require the exact next sequence number."""
    key = _validate_session_key(session_key)
    _validate_direction(expected_direction)
    expected_session = _token_bytes(expected_session_id)
    _validate_last_sequence(last_sequence)

    data = _require_message(document, _ENVELOPE_KEYS, "message")
    direction = _require_string(data["direction"])
    _validate_direction(direction)
    session_id = _require_string(data["session_id"])
    received_session = _token_bytes(session_id)
    sequence = data["sequence"]
    _validate_positive_sequence(sequence)
    payload = _normalize_payload(data["payload"])
    signature = _token_bytes(data["signature"])

    unsigned = _unsigned_envelope(direction, session_id, sequence, payload)
    expected_signature = hmac.new(
        key,
        _ENVELOPE_DOMAIN + canonical_json_bytes(unsigned),
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


def _require_context(context: object) -> HandshakeContext:
    """Require the validated context type."""
    if not isinstance(context, HandshakeContext):
        raise ProtocolFormatError("invalid protocol message")
    return context


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


def _unsigned_envelope(
    direction: str,
    session_id: str,
    sequence: int,
    payload: Dict[str, object],
) -> Dict[str, object]:
    """Return the exact application fields covered by the envelope MAC."""
    return {
        "version": PROTOCOL_VERSION,
        "type": "message",
        "session_id": session_id,
        "direction": direction,
        "sequence": sequence,
        "payload": payload,
    }


__all__ = [
    "DIRECTION_DOMOTICZ_TO_HA",
    "DIRECTION_HA_TO_DOMOTICZ",
    "MAX_MESSAGE_BYTES",
    "MAX_SEQUENCE",
    "NONCE_BITS",
    "PAIRING_KEY_BITS",
    "PROTOCOL_VERSION",
    "ClientHello",
    "HandshakeContext",
    "ProtocolAuthenticationError",
    "ProtocolError",
    "ProtocolFormatError",
    "ProtocolSequenceError",
    "VerifiedEnvelope",
    "accept_challenge",
    "build_authenticate",
    "build_challenge",
    "build_hello",
    "build_ready",
    "canonical_json_bytes",
    "canonical_json_dumps",
    "canonical_json_loads",
    "create_client_proof",
    "create_server_proof",
    "derive_session_id",
    "derive_session_key",
    "generate_destination_id",
    "generate_link_id",
    "generate_nonce",
    "generate_pairing_key",
    "make_handshake_context",
    "parse_hello",
    "sign_envelope",
    "validate_destination_id",
    "validate_link_id",
    "validate_nonce",
    "validate_pairing_key",
    "verify_authenticate",
    "verify_client_proof",
    "verify_envelope",
    "verify_ready",
    "verify_server_proof",
]
