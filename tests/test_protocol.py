"""Tests for the host-neutral authenticated connection protocol."""

from __future__ import annotations

import base64
import math
import re
from copy import deepcopy

import pytest

from custom_components.domoticz_sync.core import protocol
from custom_components.domoticz_sync.core.capabilities import (
    Availability,
    Capability,
    CapabilityKind,
    SourceIdentity,
)
from custom_components.domoticz_sync.core.protocol import (
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
    FEATURE_DOMOTICZ_INVENTORY_V1,
    FEATURE_HA_EXPORT_BINARY_V1,
    FEATURE_HA_EXPORT_CONTINUOUS_V1,
    FEATURE_HA_EXPORT_NUMERIC_V1,
    MAX_MESSAGE_BYTES,
    MAX_SAFE_INTEGER,
    PROTOCOL_VERSION,
    PROTOCOL_VERSION_V1,
    PROTOCOL_VERSION_V2,
    SUPPORTED_V2_FEATURES,
    SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
    WEBSOCKET_SUBPROTOCOL_V2,
    ApplyRequest,
    ApplyResult,
    ApplyResultStatus,
    ClientHello,
    HandshakeContext,
    InventoryResult,
    InventoryResultStatus,
    InventoryTarget,
    InventoryUnit,
    ProtocolAuthenticationError,
    ProtocolCompatibilityError,
    ProtocolFormatError,
    ProtocolSelection,
    ProtocolSequenceError,
    V2HandshakeContext,
    accept_challenge,
    accept_v2_challenge,
    assemble_inventory_results,
    build_application_ready,
    build_apply,
    build_apply_result,
    build_authenticate,
    build_binary_apply,
    build_binary_apply_result,
    build_challenge,
    build_hello,
    build_inventory_request,
    build_inventory_result,
    build_ready,
    build_v2_authenticate,
    build_v2_challenge,
    build_v2_hello,
    build_v2_ready,
    canonical_json_bytes,
    canonical_json_dumps,
    canonical_json_loads,
    create_client_proof,
    create_server_proof,
    create_v2_client_proof,
    create_v2_server_proof,
    derive_domoticz_target_id,
    derive_session_id,
    derive_session_key,
    derive_v2_session_id,
    derive_v2_session_key,
    generate_destination_id,
    generate_link_id,
    generate_nonce,
    generate_pairing_key,
    generate_request_id,
    make_handshake_context,
    make_v2_handshake_context,
    negotiate_features,
    parse_application_ready,
    parse_apply,
    parse_apply_result,
    parse_binary_apply,
    parse_binary_apply_result,
    parse_hello,
    parse_inventory_request,
    parse_inventory_result,
    parse_v2_hello,
    select_websocket_subprotocol,
    sign_envelope,
    validate_destination_id,
    validate_feature_ids,
    validate_link_id,
    validate_nonce,
    validate_pairing_key,
    validate_protocol_tokens,
    verify_authenticate,
    verify_client_proof,
    verify_envelope,
    verify_ready,
    verify_server_proof,
    verify_v2_authenticate,
    verify_v2_client_proof,
    verify_v2_ready,
)
from custom_components.domoticz_sync.core.reconciliation import (
    ReconciliationAction,
    ReconciliationActionKind,
)

_URLSAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _token(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _fixed_context() -> HandshakeContext:
    return HandshakeContext(
        link_id="link_test",
        destination_id="domoticz_test",
        client_nonce=_token(bytes(range(32))),
        server_nonce=_token(bytes(range(32, 64))),
    )


def _fixed_pairing_key() -> str:
    return _token(bytes(range(64, 96)))


def _session() -> tuple[bytes, HandshakeContext, str]:
    context = _fixed_context()
    session_key = derive_session_key(_fixed_pairing_key(), context)
    return session_key, context, derive_session_id(session_key, context)


def _selection(
    features: tuple[str, ...] = SUPPORTED_V2_FEATURES,
) -> ProtocolSelection:
    return ProtocolSelection(
        version=PROTOCOL_VERSION_V2,
        websocket_subprotocol=WEBSOCKET_SUBPROTOCOL_V2,
        features=features,
    )


def _fixed_v2_context(
    *,
    client_features: tuple[str, ...] = SUPPORTED_V2_FEATURES,
    server_features: tuple[str, ...] = SUPPORTED_V2_FEATURES,
) -> V2HandshakeContext:
    hello = parse_v2_hello(
        build_v2_hello(
            "link_test",
            "domoticz_test",
            _token(bytes(range(32))),
            client_protocols=(
                "unknown.example.v9",
                WEBSOCKET_SUBPROTOCOL_V2,
            ),
            selected_protocol=WEBSOCKET_SUBPROTOCOL_V2,
            client_features=client_features,
        )
    )
    return make_v2_handshake_context(
        hello,
        _token(bytes(range(32, 64))),
        server_protocols=SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
        server_features=server_features,
    )


def _v2_session(
    features: tuple[str, ...] = SUPPORTED_V2_FEATURES,
) -> tuple[bytes, V2HandshakeContext, str]:
    context = _fixed_v2_context(
        client_features=features,
        server_features=features,
    )
    session_key = derive_v2_session_key(_fixed_pairing_key(), context)
    return session_key, context, derive_v2_session_id(session_key, context)


def _source_identity() -> SourceIdentity:
    return SourceIdentity(
        system="home_assistant",
        instance_id="ha-instance-1",
        object_id="sensor.living_room_temperature",
        capability_id="state",
    )


def _numeric_capability(
    *,
    availability: Availability = Availability.AVAILABLE,
    value: object = 21.5,
    state_class: str | None = "measurement",
) -> Capability:
    return Capability(
        source=_source_identity(),
        kind=CapabilityKind.NUMERIC,
        name="Living room temperature",
        value=value,
        availability=availability,
        semantic="temperature",
        unit="\N{DEGREE SIGN}C",
        state_class=state_class,
    )


def _binary_source_identity() -> SourceIdentity:
    return SourceIdentity(
        system="home_assistant",
        instance_id="ha-instance-1",
        object_id="binary_sensor.living_room_motion",
        capability_id="state",
    )


def _binary_capability(
    *,
    availability: Availability = Availability.AVAILABLE,
    value: object = True,
    semantic: str | None = "motion",
) -> Capability:
    return Capability(
        source=_binary_source_identity(),
        kind=CapabilityKind.BINARY,
        name="Living room motion",
        value=value,
        availability=availability,
        semantic=semantic,
    )


def _inventory_selection() -> ProtocolSelection:
    return _selection((FEATURE_DOMOTICZ_INVENTORY_V1,))


def _inventory_unit(
    *,
    unit: int = 1,
    name: str = "Living room motion",
    n_value: int = 0,
    s_value: str = "Off",
    custom_option: str | None = None,
    has_other_options: bool = False,
) -> InventoryUnit:
    return InventoryUnit(
        unit=unit,
        name=name,
        type=244,
        subtype=73,
        switch_type=8,
        used=True,
        n_value=n_value,
        s_value=s_value,
        custom_option=custom_option,
        has_other_options=has_other_options,
    )


def _inventory_target(
    target_id: str = "HA00000000000000000000001",
    *,
    timed_out: bool = False,
    units: tuple[InventoryUnit, ...] | None = None,
) -> InventoryTarget:
    return InventoryTarget(
        target_id=target_id,
        timed_out=timed_out,
        units=(_inventory_unit(),) if units is None else units,
    )


def _inventory_result(
    *,
    request_id: str = "inventory-1",
    status: InventoryResultStatus = InventoryResultStatus.CONFIRMED,
    page: int = 1,
    complete: bool = True,
    targets: tuple[InventoryTarget, ...] | None = None,
) -> InventoryResult:
    return InventoryResult(
        request_id=request_id,
        status=status,
        page=page,
        complete=complete,
        targets=(_inventory_target(),) if targets is None else targets,
    )


def test_generated_pairing_keys_are_strong_canonical_and_distinct() -> None:
    """Pairing material is 256 random bits encoded without padding."""
    first = generate_pairing_key()
    second = generate_pairing_key()

    assert first != second
    assert _URLSAFE_TOKEN_RE.fullmatch(first)
    assert len(base64.urlsafe_b64decode(first + "=")) == 32
    assert validate_pairing_key(first) is None


def test_generated_nonces_are_strong_canonical_and_distinct() -> None:
    """Each side can contribute an independent 256-bit fresh nonce."""
    first = generate_nonce()
    second = generate_nonce()

    assert first != second
    assert _URLSAFE_TOKEN_RE.fullmatch(first)
    assert len(base64.urlsafe_b64decode(first + "=")) == 32
    assert validate_nonce(first) is None


def test_generated_request_ids_are_strong_valid_and_distinct() -> None:
    """Correlation IDs retain nonce entropy and always satisfy their schema."""
    first = generate_request_id()
    second = generate_request_id()

    assert first != second
    assert first.startswith("request_")
    assert _URLSAFE_TOKEN_RE.fullmatch(first.removeprefix("request_"))
    assert len(base64.urlsafe_b64decode(first.removeprefix("request_") + "=")) == 32
    assert _REQUEST_ID_RE.fullmatch(first)


@pytest.mark.parametrize(
    ("first_byte", "raw_prefix"),
    ((0xF8, "-"), (0xFC, "_")),
)
def test_generated_request_ids_prefix_problematic_raw_tokens(
    monkeypatch: pytest.MonkeyPatch,
    first_byte: int,
    raw_prefix: str,
) -> None:
    """A raw base64url leader cannot make a generated request ID invalid."""
    random_bytes = bytes([first_byte]) + bytes(31)
    raw_token = _token(random_bytes)
    assert raw_token.startswith(raw_prefix)
    monkeypatch.setattr(
        protocol.secrets,
        "token_bytes",
        lambda size: random_bytes if size == 32 else bytes(size),
    )

    request_id = generate_request_id()

    assert request_id == f"request_{raw_token}"
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    assert build_apply(_selection(), request_id, action)["request_id"] == request_id


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "short",
        "A" * 42,
        "A" * 44,
        "A" * 42 + "=",
        "+" + "A" * 42,
        "/" + "A" * 42,
    ],
)
def test_pairing_keys_and_nonces_reject_noncanonical_values(value: object) -> None:
    """Malformed secret material fails with one non-reflective error."""
    for validator in (validate_pairing_key, validate_nonce):
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            validator(value)


def test_generated_identifiers_are_opaque_and_valid() -> None:
    """Both configuration identities are generated without unsafe characters."""
    link_id = generate_link_id()
    destination_id = generate_destination_id()

    assert link_id.startswith("link_")
    assert destination_id.startswith("domoticz_")
    assert validate_link_id(link_id) is None
    assert validate_destination_id(destination_id) is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "-leading",
        "contains space",
        "contains/slash",
        "line\nbreak",
        "x" * 129,
    ],
)
def test_identifiers_reject_unsafe_values(value: object) -> None:
    """Handshake identities are bounded conservative ASCII tokens."""
    for validator in (validate_link_id, validate_destination_id):
        with pytest.raises(ProtocolFormatError):
            validator(value)


def test_canonical_json_is_sorted_compact_ascii_and_deterministic() -> None:
    """Both runtimes derive identical bytes from the same JSON value."""
    value = {
        "z": [True, None, 1.5],
        "a": "temperatuur \N{DEGREE SIGN}",
    }

    encoded = '{"a":"temperatuur \\u00b0","z":[true,null,1.5]}'
    assert canonical_json_dumps(value) == encoded
    assert canonical_json_bytes(value) == encoded.encode("ascii")
    assert canonical_json_loads(encoded) == value
    assert canonical_json_loads(encoded.encode("ascii")) == value


@pytest.mark.parametrize(
    "document",
    [
        "",
        " ",
        '{"b":1, "a":2}',
        '{"b":1,"a":2}',
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":Infinity}',
        '{"a":1}\n',
        '{"a":"°"}',
        b"\xff",
    ],
)
def test_canonical_json_loader_rejects_noncanonical_or_invalid_input(
    document: object,
) -> None:
    """Only the single deterministic wire representation is accepted."""
    with pytest.raises(
        ProtocolFormatError,
        match="^invalid protocol message$",
    ):
        canonical_json_loads(document)


@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
        MAX_SAFE_INTEGER + 1,
        {1: "non-string key"},
        ("tuple",),
        object(),
        "\ud800",
    ],
)
def test_canonical_json_dumper_rejects_non_interoperable_values(
    value: object,
) -> None:
    """Unsupported values cannot enter a signed transcript or payload."""
    with pytest.raises(
        ProtocolFormatError,
        match="^invalid protocol message$",
    ):
        canonical_json_dumps(value)


def test_canonical_json_rejects_excessive_nesting() -> None:
    """Nesting is bounded before recursive JSON encoding."""
    value: object = "leaf"
    for _ in range(protocol.MAX_JSON_DEPTH + 1):
        value = [value]

    with pytest.raises(ProtocolFormatError):
        canonical_json_dumps(value)


def test_complete_handshake_authenticates_both_sides_and_agrees_on_session() -> None:
    """The four messages establish one shared key and secret-bound session ID."""
    pairing_key = _fixed_pairing_key()
    hello_document = build_hello(
        "link_test",
        "domoticz_test",
        _token(bytes(range(32))),
    )
    hello = parse_hello(canonical_json_loads(canonical_json_dumps(hello_document)))

    server_context = make_handshake_context(
        hello,
        _token(bytes(range(32, 64))),
    )
    challenge = build_challenge(pairing_key, server_context)
    client_context = accept_challenge(
        pairing_key,
        hello,
        canonical_json_loads(canonical_json_dumps(challenge)),
    )
    authentication = build_authenticate(pairing_key, client_context)
    verify_authenticate(
        pairing_key,
        server_context,
        canonical_json_loads(canonical_json_dumps(authentication)),
    )

    server_key = derive_session_key(pairing_key, server_context)
    client_key = derive_session_key(pairing_key, client_context)
    assert server_key == client_key

    ready = build_ready(server_key, server_context)
    assert verify_ready(client_key, client_context, ready) == ready["session_id"]


def test_fixed_handshake_has_stable_cross_runtime_vectors() -> None:
    """Domain separators and canonical transcript cannot drift unnoticed."""
    pairing_key = _fixed_pairing_key()
    context = _fixed_context()

    assert create_client_proof(pairing_key, context) == (
        "w4fIO6FJ3wP3wWgiLKnRHdmJ8A7jt6vtzNpASyXm1p0"
    )
    assert create_server_proof(pairing_key, context) == (
        "hlsjlLvMtRRwtndj2bUfD0V9OkOS2LseWSwkebxDO-4"
    )
    session_key = derive_session_key(pairing_key, context)
    assert session_key.hex() == (
        "1f75afdda01ec5e9caf3463f8ce1c69fdeb15c483b9548ea2630bf29d51d63d5"
    )
    assert (
        derive_session_id(session_key, context)
        == "xV7Ghtv1j6ZkR-G8_HrZDHQVaHKbn15herQ4Y5uNo9A"
    )


def test_v1_wire_documents_remain_byte_for_byte_stable() -> None:
    """Released v1 handshake and envelope bytes remain a frozen legacy codec."""
    assert PROTOCOL_VERSION == PROTOCOL_VERSION_V1 == 1

    pairing_key = _fixed_pairing_key()
    context = _fixed_context()
    hello = ClientHello(
        context.link_id,
        context.destination_id,
        context.client_nonce,
    )
    session_key = derive_session_key(pairing_key, context)
    session_id = derive_session_id(session_key, context)

    assert canonical_json_dumps(
        build_hello(hello.link_id, hello.destination_id, hello.client_nonce)
    ) == canonical_json_dumps(
        {
            "version": 1,
            "type": "hello",
            "link_id": "link_test",
            "destination_id": "domoticz_test",
            "client_nonce": context.client_nonce,
        }
    )
    assert build_challenge(pairing_key, context) == {
        "version": 1,
        "type": "challenge",
        "server_nonce": context.server_nonce,
        "server_proof": "hlsjlLvMtRRwtndj2bUfD0V9OkOS2LseWSwkebxDO-4",
    }
    assert build_authenticate(pairing_key, context) == {
        "version": 1,
        "type": "authenticate",
        "client_proof": "w4fIO6FJ3wP3wWgiLKnRHdmJ8A7jt6vtzNpASyXm1p0",
    }
    assert build_ready(session_key, context) == {
        "version": 1,
        "type": "ready",
        "session_id": session_id,
    }
    assert sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    ) == {
        "version": 1,
        "type": "message",
        "session_id": session_id,
        "direction": DIRECTION_DOMOTICZ_TO_HA,
        "sequence": 1,
        "payload": {"type": "ping"},
        "signature": "RWOhPNBsaGUxU76MGSp8Fen2l3e5NEF3H7LTtB7iHwQ",
    }


def test_client_and_server_proofs_are_role_separated() -> None:
    """A valid proof for one peer cannot authenticate the other peer."""
    pairing_key = _fixed_pairing_key()
    context = _fixed_context()
    client_proof = create_client_proof(pairing_key, context)
    server_proof = create_server_proof(pairing_key, context)

    assert client_proof != server_proof
    with pytest.raises(ProtocolAuthenticationError):
        verify_client_proof(pairing_key, context, server_proof)
    with pytest.raises(ProtocolAuthenticationError):
        verify_server_proof(pairing_key, context, client_proof)


def test_proofs_bind_all_identity_and_freshness_fields() -> None:
    """Changing any transcript identity or nonce invalidates its proof."""
    pairing_key = _fixed_pairing_key()
    context = _fixed_context()
    proof = create_server_proof(pairing_key, context)
    changed_contexts = [
        HandshakeContext(
            "link_other",
            context.destination_id,
            context.client_nonce,
            context.server_nonce,
        ),
        HandshakeContext(
            context.link_id,
            "domoticz_other",
            context.client_nonce,
            context.server_nonce,
        ),
        HandshakeContext(
            context.link_id,
            context.destination_id,
            generate_nonce(),
            context.server_nonce,
        ),
        HandshakeContext(
            context.link_id,
            context.destination_id,
            context.client_nonce,
            generate_nonce(),
        ),
    ]

    for changed in changed_contexts:
        with pytest.raises(ProtocolAuthenticationError):
            verify_server_proof(pairing_key, changed, proof)


def test_proofs_reject_a_different_pairing_key() -> None:
    """Possession of public handshake fields cannot produce valid proofs."""
    context = _fixed_context()
    proof = create_client_proof(_fixed_pairing_key(), context)

    with pytest.raises(ProtocolAuthenticationError):
        verify_client_proof(generate_pairing_key(), context, proof)


def test_handshake_message_schemas_are_exact() -> None:
    """Extra, missing, wrong-version, and wrong-type fields fail closed."""
    hello = build_hello(
        "link_test",
        "domoticz_test",
        _fixed_context().client_nonce,
    )
    mutations = []
    extra = dict(hello)
    extra["unexpected"] = True
    mutations.append(extra)
    missing = dict(hello)
    del missing["client_nonce"]
    mutations.append(missing)
    wrong_version = dict(hello)
    wrong_version["version"] = PROTOCOL_VERSION + 1
    mutations.append(wrong_version)
    bool_version = dict(hello)
    bool_version["version"] = True
    mutations.append(bool_version)
    wrong_type = dict(hello)
    wrong_type["type"] = "challenge"
    mutations.append(wrong_type)

    for mutation in mutations:
        with pytest.raises(ProtocolFormatError):
            parse_hello(mutation)


def test_each_handshake_stage_rejects_extra_fields() -> None:
    """Strict schemas apply after the hello as well."""
    pairing_key = _fixed_pairing_key()
    context = _fixed_context()
    hello = ClientHello(
        context.link_id,
        context.destination_id,
        context.client_nonce,
    )
    challenge = build_challenge(pairing_key, context)
    authentication = build_authenticate(pairing_key, context)
    ready = build_ready(derive_session_key(pairing_key, context), context)

    for document, action in [
        (
            challenge,
            lambda changed: accept_challenge(pairing_key, hello, changed),
        ),
        (
            authentication,
            lambda changed: verify_authenticate(pairing_key, context, changed),
        ),
        (
            ready,
            lambda changed: verify_ready(
                derive_session_key(pairing_key, context),
                context,
                changed,
            ),
        ),
    ]:
        changed = dict(document)
        changed["unexpected"] = True
        with pytest.raises(ProtocolFormatError):
            action(changed)


def test_ready_message_is_bound_to_the_secret_session_key() -> None:
    """An observer of both nonces cannot forge the final ready message."""
    context = _fixed_context()
    session_key = derive_session_key(_fixed_pairing_key(), context)
    ready = build_ready(session_key, context)

    with pytest.raises(ProtocolAuthenticationError):
        verify_ready(bytes(32), context, ready)


def test_v2_protocol_and_feature_validators_are_bounded_and_deterministic() -> None:
    """Negotiation permits future identifiers without permitting ambiguity."""
    protocols = ("future.example.v9", WEBSOCKET_SUBPROTOCOL_V2)
    features = ("future.feature", FEATURE_HA_EXPORT_NUMERIC_V1)

    assert validate_protocol_tokens(protocols) == protocols
    assert validate_feature_ids(features) == features
    assert (
        select_websocket_subprotocol(
            protocols,
            SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
        )
        == WEBSOCKET_SUBPROTOCOL_V2
    )
    assert negotiate_features(
        features,
        (FEATURE_HA_EXPORT_NUMERIC_V1, "server.feature"),
    ) == (FEATURE_HA_EXPORT_NUMERIC_V1,)

    invalid_protocols = (
        (),
        (WEBSOCKET_SUBPROTOCOL_V2, WEBSOCKET_SUBPROTOCOL_V2),
        ("contains space",),
        tuple(f"protocol-{index}" for index in range(protocol.MAX_PROTOCOL_TOKENS + 1)),
    )
    for value in invalid_protocols:
        with pytest.raises(ProtocolFormatError):
            validate_protocol_tokens(value)

    invalid_features = (
        ("z.feature", "a.feature"),
        ("same.feature", "same.feature"),
        ("contains space",),
        tuple(f"feature-{index:03d}" for index in range(protocol.MAX_FEATURE_IDS + 1)),
    )
    for value in invalid_features:
        with pytest.raises(ProtocolFormatError):
            validate_feature_ids(value)


def test_complete_v2_handshake_negotiates_features_and_agrees_on_session() -> None:
    """The v2 handshake authenticates the HTTP offer and feature intersection."""
    pairing_key = _fixed_pairing_key()
    hello_document = build_v2_hello(
        "link_test",
        "domoticz_test",
        _fixed_context().client_nonce,
        client_protocols=("future.example.v9", WEBSOCKET_SUBPROTOCOL_V2),
        selected_protocol=WEBSOCKET_SUBPROTOCOL_V2,
        client_features=(
            "future.feature",
            FEATURE_HA_EXPORT_NUMERIC_V1,
        ),
    )
    assert hello_document == {
        "version": 2,
        "type": "hello",
        "link_id": "link_test",
        "destination_id": "domoticz_test",
        "client_nonce": _fixed_context().client_nonce,
        "client_protocols": [
            "future.example.v9",
            WEBSOCKET_SUBPROTOCOL_V2,
        ],
        "selected_protocol": WEBSOCKET_SUBPROTOCOL_V2,
        "client_features": [
            "future.feature",
            FEATURE_HA_EXPORT_NUMERIC_V1,
        ],
    }
    hello = parse_v2_hello(canonical_json_loads(canonical_json_dumps(hello_document)))
    server_context = make_v2_handshake_context(
        hello,
        _fixed_context().server_nonce,
        server_protocols=SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
        server_features=(
            FEATURE_HA_EXPORT_NUMERIC_V1,
            "server.feature",
        ),
    )
    assert server_context.selection == _selection((FEATURE_HA_EXPORT_NUMERIC_V1,))

    challenge = build_v2_challenge(pairing_key, server_context)
    assert set(challenge) == {
        "version",
        "type",
        "server_nonce",
        "server_protocols",
        "selected_protocol",
        "server_features",
        "selected_features",
        "server_proof",
    }
    client_context = accept_v2_challenge(
        pairing_key,
        hello,
        canonical_json_loads(canonical_json_dumps(challenge)),
    )
    assert client_context == server_context

    authentication = build_v2_authenticate(pairing_key, client_context)
    verify_v2_authenticate(pairing_key, server_context, authentication)
    client_key = derive_v2_session_key(pairing_key, client_context)
    server_key = derive_v2_session_key(pairing_key, server_context)
    assert client_key == server_key

    ready = build_v2_ready(server_key, server_context)
    assert verify_v2_ready(client_key, client_context, ready) == ready["session_id"]


@pytest.mark.parametrize(
    ("client_features", "server_features", "selected_features"),
    [
        (
            (FEATURE_HA_EXPORT_NUMERIC_V1,),
            SUPPORTED_V2_FEATURES,
            (FEATURE_HA_EXPORT_NUMERIC_V1,),
        ),
        (
            SUPPORTED_V2_FEATURES,
            (FEATURE_HA_EXPORT_NUMERIC_V1,),
            (FEATURE_HA_EXPORT_NUMERIC_V1,),
        ),
        (
            (FEATURE_HA_EXPORT_BINARY_V1,),
            SUPPORTED_V2_FEATURES,
            (FEATURE_HA_EXPORT_BINARY_V1,),
        ),
        (
            SUPPORTED_V2_FEATURES,
            SUPPORTED_V2_FEATURES,
            SUPPORTED_V2_FEATURES,
        ),
    ],
)
def test_v2_mixed_versions_negotiate_features_independently(
    client_features: tuple[str, ...],
    server_features: tuple[str, ...],
    selected_features: tuple[str, ...],
) -> None:
    """New and old peers retain exactly their mutually supported behavior."""
    context = _fixed_v2_context(
        client_features=client_features,
        server_features=server_features,
    )

    assert context.selection.features == selected_features
    assert context.selection.supports(FEATURE_HA_EXPORT_BINARY_V1) is (
        FEATURE_HA_EXPORT_BINARY_V1 in selected_features
    )
    assert context.selection.supports(FEATURE_HA_EXPORT_NUMERIC_V1) is (
        FEATURE_HA_EXPORT_NUMERIC_V1 in selected_features
    )


def test_v2_fixed_handshake_has_stable_cross_runtime_vectors() -> None:
    """V2 domains and the complete canonical negotiation cannot drift."""
    pairing_key = _fixed_pairing_key()
    context = _fixed_v2_context(
        client_features=(FEATURE_HA_EXPORT_NUMERIC_V1,),
        server_features=(FEATURE_HA_EXPORT_NUMERIC_V1,),
    )

    assert (
        create_v2_client_proof(pairing_key, context)
        == "iXesG4Fg1IV1Jy5RvwyA6ysgm2gXV7GoDRA3J9g0oTs"
    )
    assert (
        create_v2_server_proof(pairing_key, context)
        == "SA9Bf4xY1s_Hg7g7YZ4YLAC73SDwhnlQncDhZat_jKI"
    )
    session_key = derive_v2_session_key(pairing_key, context)
    assert session_key.hex() == (
        "7ab23a78a617784d031cf26800b5cd200316c4abde67cd3712520cbf4f73522f"
    )
    assert (
        derive_v2_session_id(session_key, context)
        == "q0lalHuXUc47TorXOZpdpOho3PppSuRGL8pydua5Jko"
    )


def test_v2_server_proof_binds_client_offer_and_complete_selection() -> None:
    """Stripping offers or mutually changing selected values fails authentication."""
    pairing_key = _fixed_pairing_key()
    original_document = build_v2_hello(
        "link_test",
        "domoticz_test",
        _fixed_context().client_nonce,
        client_protocols=("future.example.v9", WEBSOCKET_SUBPROTOCOL_V2),
        selected_protocol=WEBSOCKET_SUBPROTOCOL_V2,
        client_features=SUPPORTED_V2_FEATURES,
    )
    original_hello = parse_v2_hello(original_document)
    stripped_document = deepcopy(original_document)
    stripped_document["client_protocols"] = [WEBSOCKET_SUBPROTOCOL_V2]
    stripped_hello = parse_v2_hello(stripped_document)
    stripped_context = make_v2_handshake_context(
        stripped_hello,
        _fixed_context().server_nonce,
        server_protocols=SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
        server_features=SUPPORTED_V2_FEATURES,
    )

    with pytest.raises(ProtocolAuthenticationError):
        accept_v2_challenge(
            pairing_key,
            original_hello,
            build_v2_challenge(pairing_key, stripped_context),
        )

    context = _fixed_v2_context(
        client_features=(
            "future.feature",
            FEATURE_HA_EXPORT_NUMERIC_V1,
        )
    )
    challenge = build_v2_challenge(pairing_key, context)
    changed = deepcopy(challenge)
    changed["server_features"] = []
    changed["selected_features"] = []
    with pytest.raises(ProtocolAuthenticationError):
        accept_v2_challenge(
            pairing_key,
            parse_v2_hello(
                build_v2_hello(
                    context.link_id,
                    context.destination_id,
                    context.client_nonce,
                    client_protocols=context.client_protocols,
                    selected_protocol=context.selected_protocol,
                    client_features=context.client_features,
                )
            ),
            changed,
        )


def test_v2_client_proof_and_session_key_bind_the_selected_features() -> None:
    """The client confirmation and derived session cannot reuse another selection."""
    pairing_key = _fixed_pairing_key()
    feature_context = _fixed_v2_context()
    heartbeat_only_context = _fixed_v2_context(
        client_features=(),
        server_features=(),
    )
    proof = create_v2_client_proof(pairing_key, feature_context)

    with pytest.raises(ProtocolAuthenticationError):
        verify_v2_client_proof(pairing_key, heartbeat_only_context, proof)
    assert derive_v2_session_key(
        pairing_key,
        feature_context,
    ) != derive_v2_session_key(pairing_key, heartbeat_only_context)


def test_v2_handshake_rejects_non_deterministic_or_extended_schemas() -> None:
    """Selected protocol, feature intersection, and exact keys cannot diverge."""
    pairing_key = _fixed_pairing_key()
    context = _fixed_v2_context()
    hello = parse_v2_hello(
        build_v2_hello(
            context.link_id,
            context.destination_id,
            context.client_nonce,
            client_protocols=context.client_protocols,
            selected_protocol=context.selected_protocol,
            client_features=context.client_features,
        )
    )
    challenge = build_v2_challenge(pairing_key, context)
    mutations = []
    extra = deepcopy(challenge)
    extra["unexpected"] = True
    mutations.append(extra)
    wrong_version = deepcopy(challenge)
    wrong_version["version"] = 1
    mutations.append(wrong_version)
    wrong_selection = deepcopy(challenge)
    wrong_selection["selected_features"] = []
    mutations.append(wrong_selection)
    wrong_protocol = deepcopy(challenge)
    wrong_protocol["selected_protocol"] = "future.example.v9"
    mutations.append(wrong_protocol)

    for mutation in mutations:
        with pytest.raises(ProtocolFormatError):
            accept_v2_challenge(pairing_key, hello, mutation)


def test_signed_envelope_round_trip_and_defensive_payload_copy() -> None:
    """A valid canonical frame returns its authenticated application fields."""
    session_key, _, session_id = _session()
    payload: dict[str, object] = {
        "type": "ping",
        "details": {"counter": 1},
    }
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload=payload,
    )
    payload["type"] = "changed-after-signing"
    decoded = canonical_json_loads(canonical_json_dumps(envelope))

    verified = verify_envelope(
        session_key,
        decoded,
        protocol_version=PROTOCOL_VERSION,
        expected_direction=DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=0,
    )

    assert verified.session_id == session_id
    assert verified.direction == DIRECTION_DOMOTICZ_TO_HA
    assert verified.sequence == 1
    assert verified.payload == {
        "type": "ping",
        "details": {"counter": 1},
    }


def test_v2_envelope_uses_selected_version_and_domain() -> None:
    """V2 messages round-trip only under the authenticated v2 MAC domain."""
    context = _fixed_v2_context(
        client_features=(FEATURE_HA_EXPORT_NUMERIC_V1,),
        server_features=(FEATURE_HA_EXPORT_NUMERIC_V1,),
    )
    session_key = derive_v2_session_key(_fixed_pairing_key(), context)
    session_id = derive_v2_session_id(session_key, context)
    payload = build_application_ready(context.selection)
    envelope = sign_envelope(
        session_key,
        protocol_version=context.selection.version,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload=payload,
    )

    assert envelope["version"] == PROTOCOL_VERSION_V2
    assert envelope["signature"] == ("dJWy7BtKBLFM8V3KGKwofZi1s1GBRkd6DRBpSlNedUQ")
    assert (
        verify_envelope(
            session_key,
            envelope,
            protocol_version=PROTOCOL_VERSION_V2,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=0,
        ).payload
        == payload
    )

    with pytest.raises(ProtocolFormatError):
        verify_envelope(
            session_key,
            envelope,
            protocol_version=PROTOCOL_VERSION,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=0,
        )

    wrong_domain = deepcopy(envelope)
    wrong_domain["version"] = PROTOCOL_VERSION
    with pytest.raises(ProtocolAuthenticationError):
        verify_envelope(
            session_key,
            wrong_domain,
            protocol_version=PROTOCOL_VERSION,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=0,
        )


def test_envelope_requires_an_explicit_supported_protocol_version() -> None:
    """Callers cannot silently fall back to a global wire version."""
    session_key, _, session_id = _session()

    with pytest.raises(ProtocolFormatError):
        sign_envelope(
            session_key,
            protocol_version=3,
            direction=DIRECTION_DOMOTICZ_TO_HA,
            session_id=session_id,
            sequence=1,
            payload={"type": "ping"},
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("direction", DIRECTION_HA_TO_DOMOTICZ),
        ("session_id", _token(bytes(reversed(range(32))))),
        ("sequence", 2),
        ("payload", {"type": "changed"}),
    ],
)
def test_envelope_signature_binds_every_routing_and_payload_field(
    field: str,
    replacement: object,
) -> None:
    """Changing any signed field is detected before sequence processing."""
    session_key, _, session_id = _session()
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    )
    envelope[field] = replacement

    with pytest.raises(
        ProtocolAuthenticationError,
        match="^protocol authentication failed$",
    ):
        verify_envelope(
            session_key,
            envelope,
            protocol_version=PROTOCOL_VERSION,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=0,
        )


def test_envelope_rejects_wrong_session_key_direction_or_session() -> None:
    """An authenticated frame is accepted only in its intended channel."""
    session_key, _, session_id = _session()
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    )

    for key, direction, expected_id in [
        (bytes(32), DIRECTION_DOMOTICZ_TO_HA, session_id),
        (session_key, DIRECTION_HA_TO_DOMOTICZ, session_id),
        (session_key, DIRECTION_DOMOTICZ_TO_HA, _token(bytes(32))),
    ]:
        with pytest.raises(ProtocolAuthenticationError):
            verify_envelope(
                key,
                envelope,
                protocol_version=PROTOCOL_VERSION,
                expected_direction=direction,
                expected_session_id=expected_id,
                last_sequence=0,
            )


@pytest.mark.parametrize("last_sequence", [1, 2])
def test_envelope_rejects_replay_and_sequence_gaps(last_sequence: int) -> None:
    """Ordered WebSocket frames must advance by exactly one."""
    session_key, _, session_id = _session()
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    )

    with pytest.raises(
        ProtocolSequenceError,
        match="^invalid protocol sequence$",
    ):
        verify_envelope(
            session_key,
            envelope,
            protocol_version=PROTOCOL_VERSION,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=last_sequence,
        )


@pytest.mark.parametrize("sequence", [None, True, 0, -1, 1.0, MAX_SAFE_INTEGER + 1])
def test_sign_envelope_requires_a_positive_safe_integer_sequence(
    sequence: object,
) -> None:
    """Booleans, non-integers, zero, negatives, and overflow are invalid."""
    session_key, _, session_id = _session()

    with pytest.raises(ProtocolFormatError):
        sign_envelope(
            session_key,
            protocol_version=PROTOCOL_VERSION,
            direction=DIRECTION_DOMOTICZ_TO_HA,
            session_id=session_id,
            sequence=sequence,
            payload={"type": "ping"},
        )


def test_envelope_schema_and_payload_are_strict() -> None:
    """Unsigned fields, omitted fields, and non-object payloads fail closed."""
    session_key, _, session_id = _session()
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    )
    extra = dict(envelope)
    extra["ignored"] = True
    missing = dict(envelope)
    del missing["signature"]

    for changed in (extra, missing):
        with pytest.raises(ProtocolFormatError):
            verify_envelope(
                session_key,
                changed,
                protocol_version=PROTOCOL_VERSION,
                expected_direction=DIRECTION_DOMOTICZ_TO_HA,
                expected_session_id=session_id,
                last_sequence=0,
            )

    with pytest.raises(ProtocolFormatError):
        sign_envelope(
            session_key,
            protocol_version=PROTOCOL_VERSION,
            direction=DIRECTION_DOMOTICZ_TO_HA,
            session_id=session_id,
            sequence=1,
            payload=["not", "an", "object"],
        )


def test_envelope_signature_is_checked_before_sequence() -> None:
    """An unauthenticated sequence cannot trigger replay handling."""
    session_key, _, session_id = _session()
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    )
    envelope["sequence"] = 2

    with pytest.raises(ProtocolAuthenticationError):
        verify_envelope(
            session_key,
            envelope,
            protocol_version=PROTOCOL_VERSION,
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=1,
        )


def test_proof_and_signature_verification_use_constant_time_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authentication comparisons go through hmac.compare_digest."""
    calls = []
    original = protocol.hmac.compare_digest

    def recording_compare(first: object, second: object) -> bool:
        calls.append((first, second))
        return original(first, second)

    monkeypatch.setattr(protocol.hmac, "compare_digest", recording_compare)
    pairing_key = _fixed_pairing_key()
    context = _fixed_context()
    verify_client_proof(
        pairing_key,
        context,
        create_client_proof(pairing_key, context),
    )
    session_key = derive_session_key(pairing_key, context)
    session_id = derive_session_id(session_key, context)
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    )
    verify_envelope(
        session_key,
        envelope,
        protocol_version=PROTOCOL_VERSION,
        expected_direction=DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=0,
    )

    assert len(calls) >= 3
    assert all(type(first) is bytes for first, _ in calls)
    assert all(type(second) is bytes for _, second in calls)


def test_public_errors_never_reflect_secret_or_attacker_controlled_values() -> None:
    """Safe generic errors can be logged without disclosing credentials."""
    pairing_key = _fixed_pairing_key()
    context = _fixed_context()
    proof = create_client_proof(pairing_key, context)
    changed_proof = ("A" if proof[0] != "A" else "B") + proof[1:]

    with pytest.raises(ProtocolAuthenticationError) as captured:
        verify_client_proof(pairing_key, context, changed_proof)

    message = str(captured.value)
    assert message == "protocol authentication failed"
    assert pairing_key not in message
    assert proof not in message
    assert changed_proof not in message


def test_mutating_an_envelope_copy_does_not_mutate_original() -> None:
    """Test helpers do not accidentally conceal shared nested state."""
    session_key, _, session_id = _session()
    envelope = sign_envelope(
        session_key,
        protocol_version=PROTOCOL_VERSION,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping", "nested": {"value": 1}},
    )
    changed = deepcopy(envelope)
    changed["payload"]["nested"]["value"] = 2

    assert envelope["payload"]["nested"]["value"] == 1


def test_protocol_message_limit_matches_the_websocket_transport() -> None:
    """Application and transport layers share one unambiguous size ceiling."""
    assert MAX_MESSAGE_BYTES == 64 * 1024
    at_limit = '"' + ("a" * (MAX_MESSAGE_BYTES - 2)) + '"'

    assert canonical_json_loads(at_limit) == "a" * (MAX_MESSAGE_BYTES - 2)
    assert canonical_json_dumps("a" * (MAX_MESSAGE_BYTES - 2)) == at_limit

    with pytest.raises(ProtocolFormatError):
        canonical_json_loads(at_limit + " ")
    with pytest.raises(ProtocolFormatError):
        canonical_json_dumps("a" * (MAX_MESSAGE_BYTES - 1))


def test_application_ready_is_exact_and_does_not_require_optional_features() -> None:
    """A v2 session can reach heartbeat-only readiness without export support."""
    selection = _selection(())
    payload = build_application_ready(selection)

    assert payload == {"schema": 1, "type": "application_ready"}
    assert parse_application_ready(selection, payload) is None

    mutations = []
    extra = deepcopy(payload)
    extra["unexpected"] = True
    mutations.append(extra)
    missing_schema = deepcopy(payload)
    del missing_schema["schema"]
    mutations.append(missing_schema)
    wrong_schema = deepcopy(payload)
    wrong_schema["schema"] = 2
    mutations.append(wrong_schema)
    boolean_schema = deepcopy(payload)
    boolean_schema["schema"] = True
    mutations.append(boolean_schema)

    for mutation in mutations:
        with pytest.raises(ProtocolFormatError):
            parse_application_ready(selection, mutation)


def test_inventory_feature_negotiates_independently_for_mixed_v2_peers() -> None:
    """Inventory is selected only when both authenticated peers advertise it."""
    legacy_features = (
        FEATURE_HA_EXPORT_BINARY_V1,
        FEATURE_HA_EXPORT_NUMERIC_V1,
    )

    assert FEATURE_DOMOTICZ_INVENTORY_V1 in SUPPORTED_V2_FEATURES
    assert SUPPORTED_V2_FEATURES == tuple(sorted(SUPPORTED_V2_FEATURES))
    assert (
        _fixed_v2_context(
            client_features=SUPPORTED_V2_FEATURES,
            server_features=legacy_features,
        ).selection.features
        == legacy_features
    )
    assert (
        _fixed_v2_context(
            client_features=legacy_features,
            server_features=SUPPORTED_V2_FEATURES,
        ).selection.features
        == legacy_features
    )
    assert _fixed_v2_context(
        client_features=SUPPORTED_V2_FEATURES,
        server_features=SUPPORTED_V2_FEATURES,
    ).selection.supports(FEATURE_DOMOTICZ_INVENTORY_V1)


def test_continuous_feature_is_explicit_and_safe_during_rolling_upgrades() -> None:
    """Continuous behavior stays dormant until both peers advertise it."""
    continuous_features = tuple(
        sorted((*SUPPORTED_V2_FEATURES, FEATURE_HA_EXPORT_CONTINUOUS_V1))
    )

    assert FEATURE_HA_EXPORT_CONTINUOUS_V1 not in SUPPORTED_V2_FEATURES
    assert (
        _fixed_v2_context(
            client_features=continuous_features,
            server_features=SUPPORTED_V2_FEATURES,
        ).selection.features
        == SUPPORTED_V2_FEATURES
    )
    assert (
        _fixed_v2_context(
            client_features=SUPPORTED_V2_FEATURES,
            server_features=continuous_features,
        ).selection.features
        == SUPPORTED_V2_FEATURES
    )
    assert _fixed_v2_context(
        client_features=continuous_features,
        server_features=continuous_features,
    ).selection.supports(FEATURE_HA_EXPORT_CONTINUOUS_V1)


def test_domoticz_target_id_derivation_is_shared_and_stable() -> None:
    """Both hosts derive the released Domoticz DeviceID from exact provenance."""
    assert derive_domoticz_target_id(_source_identity()) == (
        "HAO6H4NLE3AFBHD73RPWCZMPG"
    )


def test_inventory_request_codec_is_exact_and_feature_gated() -> None:
    """Only a negotiated inventory session can exchange a bounded request."""
    selection = _inventory_selection()
    payload = build_inventory_request(selection, "inventory-1")

    assert payload == {
        "schema": 1,
        "type": "inventory_request",
        "request_id": "inventory-1",
    }
    assert parse_inventory_request(selection, payload) == "inventory-1"

    without_inventory = _selection((FEATURE_HA_EXPORT_NUMERIC_V1,))
    with pytest.raises(ProtocolCompatibilityError):
        build_inventory_request(without_inventory, "inventory-1")
    with pytest.raises(ProtocolCompatibilityError):
        parse_inventory_request(without_inventory, payload)

    mutations = []
    for key in payload:
        missing = deepcopy(payload)
        del missing[key]
        mutations.append(missing)
    extra = deepcopy(payload)
    extra["unexpected"] = True
    mutations.append(extra)
    wrong_schema = deepcopy(payload)
    wrong_schema["schema"] = True
    mutations.append(wrong_schema)
    wrong_type = deepcopy(payload)
    wrong_type["type"] = "inventory_result"
    mutations.append(wrong_type)
    invalid_request_id = deepcopy(payload)
    invalid_request_id["request_id"] = "contains space"
    mutations.append(invalid_request_id)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_inventory_request(selection, mutation)


def test_confirmed_inventory_page_round_trips_the_exact_remote_snapshot() -> None:
    """Every managed field survives the strict inventory result codec."""
    selection = _inventory_selection()
    first = _inventory_unit()
    second = InventoryUnit(
        unit=2,
        name="Outdoor temperature",
        type=243,
        subtype=31,
        switch_type=0,
        used=False,
        n_value=-1,
        s_value="12.5",
        custom_option="1;ppm",
        has_other_options=True,
    )
    result = _inventory_result(
        page=7,
        complete=False,
        targets=(
            _inventory_target(
                timed_out=True,
                units=(first, second),
            ),
        ),
    )
    payload = build_inventory_result(selection, result)

    assert payload == {
        "schema": 1,
        "type": "inventory_result",
        "request_id": "inventory-1",
        "status": "confirmed",
        "page": 7,
        "complete": False,
        "targets": [
            {
                "target_id": "HA00000000000000000000001",
                "timed_out": True,
                "units": [
                    {
                        "unit": 1,
                        "name": "Living room motion",
                        "type": 244,
                        "subtype": 73,
                        "switch_type": 8,
                        "used": True,
                        "n_value": 0,
                        "s_value": "Off",
                        "custom_option": None,
                        "has_other_options": False,
                    },
                    {
                        "unit": 2,
                        "name": "Outdoor temperature",
                        "type": 243,
                        "subtype": 31,
                        "switch_type": 0,
                        "used": False,
                        "n_value": -1,
                        "s_value": "12.5",
                        "custom_option": "1;ppm",
                        "has_other_options": True,
                    },
                ],
            },
        ],
    }
    assert parse_inventory_result(selection, payload) == result


def test_rejected_inventory_result_is_sanitized_and_not_an_empty_snapshot() -> None:
    """A remote refusal has one fixed shape and cannot mean no devices."""
    selection = _inventory_selection()
    result = _inventory_result(
        status=InventoryResultStatus.REJECTED,
        targets=(),
    )
    payload = build_inventory_result(selection, result)

    assert payload == {
        "schema": 1,
        "type": "inventory_result",
        "request_id": "inventory-1",
        "status": "rejected",
        "page": 1,
        "complete": True,
        "targets": [],
    }
    assert parse_inventory_result(selection, payload) == result
    with pytest.raises(ProtocolCompatibilityError):
        assemble_inventory_results(selection, "inventory-1", (result,))

    mutations = []
    for field, value in (
        ("page", 2),
        ("complete", False),
        (
            "targets",
            [build_inventory_result(selection, _inventory_result())["targets"][0]],
        ),
    ):
        mutation = deepcopy(payload)
        mutation[field] = value
        mutations.append(mutation)
    details = deepcopy(payload)
    details["error"] = "remote internal detail"
    mutations.append(details)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_inventory_result(selection, mutation)


def test_inventory_messages_round_trip_inside_signed_ordered_envelopes() -> None:
    """Request and response correlation is protected by the v2 session MAC."""
    session_key, context, session_id = _v2_session()
    selection = context.selection
    request_envelope = sign_envelope(
        session_key,
        protocol_version=selection.version,
        direction=DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=1,
        payload=build_inventory_request(selection, "inventory-1"),
    )
    request = verify_envelope(
        session_key,
        request_envelope,
        protocol_version=selection.version,
        expected_direction=DIRECTION_HA_TO_DOMOTICZ,
        expected_session_id=session_id,
        last_sequence=0,
    )
    assert parse_inventory_request(selection, request.payload) == "inventory-1"

    result_envelope = sign_envelope(
        session_key,
        protocol_version=selection.version,
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload=build_inventory_result(selection, _inventory_result()),
    )
    verified_result = verify_envelope(
        session_key,
        result_envelope,
        protocol_version=selection.version,
        expected_direction=DIRECTION_DOMOTICZ_TO_HA,
        expected_session_id=session_id,
        last_sequence=0,
    )
    assert (
        parse_inventory_result(selection, verified_result.payload)
        == _inventory_result()
    )

    for last_sequence in (1, 2):
        with pytest.raises(
            ProtocolSequenceError,
            match="^invalid protocol sequence$",
        ):
            verify_envelope(
                session_key,
                result_envelope,
                protocol_version=selection.version,
                expected_direction=DIRECTION_DOMOTICZ_TO_HA,
                expected_session_id=session_id,
                last_sequence=last_sequence,
            )


def test_inventory_result_parser_rejects_extensions_and_missing_fields() -> None:
    """No message, target, or unit layer has an unsigned extension point."""
    selection = _inventory_selection()
    payload = build_inventory_result(selection, _inventory_result())
    mutations = []

    for path in ((), ("targets", 0), ("targets", 0, "units", 0)):
        extra = deepcopy(payload)
        current = extra
        for component in path:
            current = current[component]
        current["unexpected"] = True
        mutations.append(extra)

    for path, key in (
        ((), "request_id"),
        ((), "complete"),
        (("targets", 0), "timed_out"),
        (("targets", 0, "units", 0), "switch_type"),
    ):
        missing = deepcopy(payload)
        current = missing
        for component in path:
            current = current[component]
        del current[key]
        mutations.append(missing)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_inventory_result(selection, mutation)


def test_inventory_result_parser_requires_exact_scalar_types() -> None:
    """Booleans and integers cannot be confused at any inventory layer."""
    selection = _inventory_selection()
    payload = build_inventory_result(selection, _inventory_result())
    mutations = []

    scalar_mutations = (
        (("schema",), True),
        (("page",), True),
        (("complete",), 1),
        (("status",), "failed"),
        (("request_id",), 1),
        (("targets", 0, "target_id"), " leading-space"),
        (("targets", 0, "timed_out"), 0),
        (("targets", 0, "units", 0, "unit"), True),
        (("targets", 0, "units", 0, "type"), True),
        (("targets", 0, "units", 0, "subtype"), 73.0),
        (("targets", 0, "units", 0, "switch_type"), "8"),
        (("targets", 0, "units", 0, "used"), 1),
        (("targets", 0, "units", 0, "n_value"), True),
        (("targets", 0, "units", 0, "name"), 1),
        (("targets", 0, "units", 0, "s_value"), None),
        (("targets", 0, "units", 0, "custom_option"), 1),
        (("targets", 0, "units", 0, "has_other_options"), 0),
    )
    for path, value in scalar_mutations:
        mutation = deepcopy(payload)
        current = mutation
        for component in path[:-1]:
            current = current[component]
        current[path[-1]] = value
        mutations.append(mutation)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_inventory_result(selection, mutation)


def test_inventory_strings_use_explicit_utf8_byte_bounds() -> None:
    """Remote strings are rejected rather than truncated at fixed byte limits."""
    assert protocol.MAX_INVENTORY_TARGET_ID_BYTES == 128
    assert protocol.MAX_INVENTORY_NAME_BYTES == 512
    assert protocol.MAX_INVENTORY_S_VALUE_BYTES == 4096
    assert protocol.MAX_INVENTORY_OPTION_BYTES == 1024

    selection = _inventory_selection()
    at_limit = _inventory_result(
        targets=(
            _inventory_target(
                "a" * protocol.MAX_INVENTORY_TARGET_ID_BYTES,
                units=(
                    _inventory_unit(
                        name="n" * protocol.MAX_INVENTORY_NAME_BYTES,
                        s_value="s" * protocol.MAX_INVENTORY_S_VALUE_BYTES,
                        custom_option="o" * protocol.MAX_INVENTORY_OPTION_BYTES,
                    ),
                ),
            ),
        ),
    )
    assert (
        parse_inventory_result(
            selection,
            build_inventory_result(selection, at_limit),
        )
        == at_limit
    )

    multibyte_at_limit = _inventory_unit(
        name="\N{LATIN SMALL LETTER E WITH ACUTE}"
        * (protocol.MAX_INVENTORY_NAME_BYTES // 2)
    )
    assert len(multibyte_at_limit.name.encode("utf-8")) == (
        protocol.MAX_INVENTORY_NAME_BYTES
    )

    invalid_factories = (
        lambda: _inventory_target("a" * (protocol.MAX_INVENTORY_TARGET_ID_BYTES + 1)),
        lambda: _inventory_unit(
            name="\N{LATIN SMALL LETTER E WITH ACUTE}"
            * ((protocol.MAX_INVENTORY_NAME_BYTES // 2) + 1)
        ),
        lambda: _inventory_unit(
            s_value="\N{LATIN SMALL LETTER E WITH ACUTE}"
            * ((protocol.MAX_INVENTORY_S_VALUE_BYTES // 2) + 1)
        ),
        lambda: _inventory_unit(
            custom_option="\N{LATIN SMALL LETTER E WITH ACUTE}"
            * ((protocol.MAX_INVENTORY_OPTION_BYTES // 2) + 1)
        ),
    )
    for factory in invalid_factories:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            factory()


def test_inventory_page_requires_deterministic_target_and_unit_order() -> None:
    """Duplicate or reordered identities cannot create an ambiguous snapshot."""
    selection = _inventory_selection()
    first_id = "HA00000000000000000000001"
    second_id = "HA00000000000000000000002"
    result = _inventory_result(
        targets=(
            _inventory_target(
                first_id,
                units=(_inventory_unit(unit=1), _inventory_unit(unit=2)),
            ),
            _inventory_target(second_id),
        ),
    )
    payload = build_inventory_result(selection, result)

    reversed_targets = deepcopy(payload)
    reversed_targets["targets"].reverse()
    duplicate_targets = deepcopy(payload)
    duplicate_targets["targets"][1]["target_id"] = first_id
    reversed_units = deepcopy(payload)
    reversed_units["targets"][0]["units"].reverse()
    duplicate_units = deepcopy(payload)
    duplicate_units["targets"][0]["units"][1]["unit"] = 1

    for mutation in (
        reversed_targets,
        duplicate_targets,
        reversed_units,
        duplicate_units,
    ):
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_inventory_result(selection, mutation)

    empty_parent = _inventory_result(
        targets=(_inventory_target(units=()),),
    )
    assert (
        parse_inventory_result(
            selection,
            build_inventory_result(selection, empty_parent),
        )
        == empty_parent
    )


def test_inventory_assembly_distinguishes_empty_from_incomplete() -> None:
    """Only a correlated terminal confirmation can authorize reconciliation."""
    selection = _inventory_selection()
    empty = _inventory_result(targets=())
    assert (
        assemble_inventory_results(
            selection,
            "inventory-1",
            (empty,),
        )
        == ()
    )

    incomplete = _inventory_result(
        complete=False,
        targets=(_inventory_target(),),
    )
    with pytest.raises(ProtocolFormatError):
        assemble_inventory_results(
            selection,
            "inventory-1",
            (incomplete,),
        )
    with pytest.raises(ProtocolFormatError):
        assemble_inventory_results(
            selection,
            "different-request",
            (empty,),
        )


def test_inventory_assembly_requires_one_contiguous_terminal_page_sequence() -> None:
    """Gaps, repeats, early terminals, and pages after completion fail closed."""
    selection = _inventory_selection()
    first = _inventory_result(
        page=1,
        complete=False,
        targets=(_inventory_target("HA00000000000000000000001"),),
    )
    second = _inventory_result(
        page=2,
        targets=(_inventory_target("HA00000000000000000000002"),),
    )
    assert (
        assemble_inventory_results(
            selection,
            "inventory-1",
            (first, second),
        )
        == first.targets + second.targets
    )

    invalid_sequences = (
        (second,),
        (first,),
        (first, _inventory_result(page=3, targets=second.targets)),
        (first, _inventory_result(page=1, targets=second.targets)),
        (_inventory_result(targets=first.targets), second),
        (
            first,
            second,
            _inventory_result(
                page=3,
                targets=(_inventory_target("HA00000000000000000000003"),),
            ),
        ),
    )
    for pages in invalid_sequences:
        with pytest.raises(ProtocolFormatError):
            assemble_inventory_results(selection, "inventory-1", pages)


def test_inventory_assembly_rejects_cross_page_identity_ambiguity() -> None:
    """Global target ordering and correlation are validated after all pages arrive."""
    selection = _inventory_selection()
    first_id = "HA00000000000000000000001"
    second_id = "HA00000000000000000000002"
    first = _inventory_result(
        page=1,
        complete=False,
        targets=(_inventory_target(first_id),),
    )
    duplicate = _inventory_result(
        page=2,
        targets=(_inventory_target(first_id),),
    )
    out_of_order_first = _inventory_result(
        page=1,
        complete=False,
        targets=(_inventory_target(second_id),),
    )
    mismatched_request = _inventory_result(
        request_id="inventory-2",
        page=2,
        targets=(_inventory_target(second_id),),
    )

    for pages in (
        (first, duplicate),
        (out_of_order_first, duplicate),
        (first, mismatched_request),
    ):
        with pytest.raises(ProtocolFormatError):
            assemble_inventory_results(selection, "inventory-1", pages)


def test_inventory_count_and_payload_bounds_are_enforced() -> None:
    """Paging remains bounded before the complete snapshot is trusted."""
    assert protocol.MAX_INVENTORY_TARGETS == 512
    assert protocol.MAX_INVENTORY_UNITS == 1024
    assert protocol.MAX_INVENTORY_PAGES == 512
    assert protocol.MAX_INVENTORY_TARGETS_PER_PAGE == 64
    assert protocol.MAX_INVENTORY_PAYLOAD_BYTES == 60 * 1024
    assert protocol.INVENTORY_TIMEOUT_SECONDS == 10

    selection = _inventory_selection()
    targets = tuple(
        _inventory_target(f"HA{index:023d}")
        for index in range(protocol.MAX_INVENTORY_TARGETS_PER_PAGE + 1)
    )
    with pytest.raises(ProtocolFormatError):
        build_inventory_result(
            selection,
            _inventory_result(targets=targets),
        )

    oversized_payload = _inventory_result(
        targets=tuple(
            _inventory_target(
                f"HA{index:023d}",
                units=(_inventory_unit(name="n", s_value="x" * 750),),
            )
            for index in range(protocol.MAX_INVENTORY_TARGETS_PER_PAGE)
        ),
    )
    with pytest.raises(ProtocolFormatError):
        build_inventory_result(selection, oversized_payload)


def test_inventory_accepts_exact_aggregate_limits_and_rejects_one_more() -> None:
    """The complete snapshot caps are inclusive and checked across pages."""
    selection = _inventory_selection()
    all_targets = tuple(
        _inventory_target(
            f"HA{index:023d}",
            units=(_inventory_unit(unit=1), _inventory_unit(unit=2)),
        )
        for index in range(protocol.MAX_INVENTORY_TARGETS)
    )
    pages = tuple(
        _inventory_result(
            page=(offset // protocol.MAX_INVENTORY_TARGETS_PER_PAGE) + 1,
            complete=(
                offset + protocol.MAX_INVENTORY_TARGETS_PER_PAGE
                == protocol.MAX_INVENTORY_TARGETS
            ),
            targets=all_targets[
                offset : offset + protocol.MAX_INVENTORY_TARGETS_PER_PAGE
            ],
        )
        for offset in range(
            0,
            protocol.MAX_INVENTORY_TARGETS,
            protocol.MAX_INVENTORY_TARGETS_PER_PAGE,
        )
    )
    wire_pages = tuple(
        parse_inventory_result(
            selection,
            build_inventory_result(selection, page),
        )
        for page in pages
    )
    assert (
        assemble_inventory_results(
            selection,
            "inventory-1",
            wire_pages,
        )
        == all_targets
    )

    too_many_units = list(all_targets)
    too_many_units[0] = _inventory_target(
        too_many_units[0].target_id,
        units=(
            _inventory_unit(unit=1),
            _inventory_unit(unit=2),
            _inventory_unit(unit=3),
        ),
    )
    pages_with_extra_unit = tuple(
        _inventory_result(
            page=(offset // protocol.MAX_INVENTORY_TARGETS_PER_PAGE) + 1,
            complete=(
                offset + protocol.MAX_INVENTORY_TARGETS_PER_PAGE
                == protocol.MAX_INVENTORY_TARGETS
            ),
            targets=tuple(
                too_many_units[
                    offset : offset + protocol.MAX_INVENTORY_TARGETS_PER_PAGE
                ]
            ),
        )
        for offset in range(
            0,
            protocol.MAX_INVENTORY_TARGETS,
            protocol.MAX_INVENTORY_TARGETS_PER_PAGE,
        )
    )
    with pytest.raises(ProtocolFormatError):
        assemble_inventory_results(
            selection,
            "inventory-1",
            pages_with_extra_unit,
        )

    last_page = pages[-1]
    too_many_targets = pages[:-1] + (
        _inventory_result(
            page=last_page.page,
            complete=False,
            targets=last_page.targets,
        ),
        _inventory_result(
            page=last_page.page + 1,
            targets=(_inventory_target(f"HA{protocol.MAX_INVENTORY_TARGETS:023d}"),),
        ),
    )
    with pytest.raises(ProtocolFormatError):
        assemble_inventory_results(
            selection,
            "inventory-1",
            too_many_targets,
        )


def test_inventory_page_count_is_capped_at_512() -> None:
    """Even minimally populated page streams have a hard overall ceiling."""
    selection = _inventory_selection()
    pages = tuple(
        _inventory_result(
            page=index + 1,
            complete=index + 1 == protocol.MAX_INVENTORY_PAGES,
            targets=(_inventory_target(f"HA{index:023d}"),),
        )
        for index in range(protocol.MAX_INVENTORY_PAGES)
    )
    assert (
        len(assemble_inventory_results(selection, "inventory-1", pages))
        == protocol.MAX_INVENTORY_TARGETS
    )

    with pytest.raises(ProtocolFormatError):
        _inventory_result(
            page=protocol.MAX_INVENTORY_PAGES + 1,
            targets=(_inventory_target(),),
        )


def test_apply_codecs_require_the_negotiated_numeric_feature() -> None:
    """Unnegotiated export messages cannot be emitted or accepted."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    payload = build_apply(_selection(), "request-1", action)

    with pytest.raises(ProtocolCompatibilityError):
        build_apply(_selection(()), "request-1", action)
    with pytest.raises(ProtocolCompatibilityError):
        parse_apply(_selection(()), payload)
    with pytest.raises(ProtocolCompatibilityError):
        build_apply_result(
            _selection(()),
            "request-1",
            ApplyResultStatus.REJECTED,
            None,
            None,
        )


def test_apply_codec_rejects_non_numeric_capabilities() -> None:
    """The negotiated numeric feature cannot transport a different entity kind."""
    binary_action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=Capability(
            source=_source_identity(),
            kind=CapabilityKind.BINARY,
            name="Living room motion",
            value=True,
        ),
    )

    with pytest.raises(ProtocolFormatError):
        build_apply(_selection(), "request-1", binary_action)

    payload = build_apply(
        _selection(),
        "request-1",
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=_numeric_capability(),
        ),
    )
    payload["action"]["capability"].update(
        kind="binary",
        value=True,
        semantic=None,
        unit=None,
        state_class=None,
    )
    with pytest.raises(ProtocolFormatError):
        parse_apply(_selection(), payload)


@pytest.mark.parametrize(
    "action",
    [
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=_numeric_capability(),
        ),
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            capability=_numeric_capability(value=22),
            target_id="42",
        ),
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            capability=_numeric_capability(
                availability=Availability.UNAVAILABLE,
                value=None,
            ),
            target_id="42",
            stale=True,
        ),
    ],
)
def test_apply_codec_round_trips_complete_reconciliation_actions(
    action: ReconciliationAction,
) -> None:
    """Every action and capability field survives the application wire format."""
    selection = _selection()
    payload = build_apply(selection, "request-42", action)

    assert payload == {
        "schema": 1,
        "type": "apply",
        "request_id": "request-42",
        "action": {
            "kind": action.kind.value,
            "capability": {
                "source": {
                    "system": "home_assistant",
                    "instance_id": "ha-instance-1",
                    "object_id": "sensor.living_room_temperature",
                    "capability_id": "state",
                },
                "kind": "numeric",
                "name": "Living room temperature",
                "value": action.capability.value,
                "availability": action.capability.availability.value,
                "semantic": "temperature",
                "unit": "\N{DEGREE SIGN}C",
                "state_class": "measurement",
            },
            "target_id": action.target_id,
            "stale": action.stale,
        },
    }
    assert parse_apply(selection, payload) == ApplyRequest(
        request_id="request-42",
        action=action,
    )


def test_apply_payload_can_be_signed_verified_and_parsed() -> None:
    """The strict request codec composes with authenticated envelopes."""
    session_key, context, session_id = _v2_session()
    selection = context.selection
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    envelope = sign_envelope(
        session_key,
        protocol_version=selection.version,
        direction=DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=1,
        payload=build_apply(selection, "request-1", action),
    )

    verified = verify_envelope(
        session_key,
        envelope,
        protocol_version=selection.version,
        expected_direction=DIRECTION_HA_TO_DOMOTICZ,
        expected_session_id=session_id,
        last_sequence=0,
    )

    assert parse_apply(selection, verified.payload).action == action


def test_apply_codec_preserves_absent_state_class_as_null() -> None:
    """The wire contract retains an explicit nullable metadata field."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(state_class=None),
    )

    selection = _selection()
    payload = build_apply(selection, "request-1", action)

    assert payload["action"]["capability"]["state_class"] is None
    assert parse_apply(selection, payload).action == action


def test_apply_parser_rejects_extra_or_missing_fields_at_every_level() -> None:
    """No unsigned extension point exists in an apply request."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    selection = _selection()
    payload = build_apply(selection, "request-1", action)
    mutations = []

    for path in (
        (),
        ("action",),
        ("action", "capability"),
        ("action", "capability", "source"),
    ):
        extra = deepcopy(payload)
        current = extra
        for component in path:
            current = current[component]
        current["unexpected"] = True
        mutations.append(extra)

    missing_request_id = deepcopy(payload)
    del missing_request_id["request_id"]
    mutations.append(missing_request_id)
    missing_schema = deepcopy(payload)
    del missing_schema["schema"]
    mutations.append(missing_schema)
    wrong_schema = deepcopy(payload)
    wrong_schema["schema"] = 2
    mutations.append(wrong_schema)
    missing_action_field = deepcopy(payload)
    del missing_action_field["action"]["stale"]
    mutations.append(missing_action_field)
    missing_capability_field = deepcopy(payload)
    del missing_capability_field["action"]["capability"]["state_class"]
    mutations.append(missing_capability_field)
    missing_source_field = deepcopy(payload)
    del missing_source_field["action"]["capability"]["source"]["instance_id"]
    mutations.append(missing_source_field)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_apply(selection, mutation)


def test_apply_parser_rejects_malformed_action_semantics() -> None:
    """Wire input cannot bypass the neutral reconciliation model."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    selection = _selection()
    payload = build_apply(selection, "request-1", action)
    mutations = []

    create_with_target = deepcopy(payload)
    create_with_target["action"]["target_id"] = "42"
    mutations.append(create_with_target)
    update_without_target = deepcopy(payload)
    update_without_target["action"]["kind"] = "update"
    mutations.append(update_without_target)
    unavailable_update = deepcopy(payload)
    unavailable_update["action"].update(kind="update", target_id="42")
    unavailable_update["action"]["capability"].update(
        availability="unavailable",
        value=None,
    )
    mutations.append(unavailable_update)
    available_mark_unavailable = deepcopy(payload)
    available_mark_unavailable["action"].update(
        kind="mark_unavailable",
        target_id="42",
    )
    mutations.append(available_mark_unavailable)
    integer_stale = deepcopy(payload)
    integer_stale["action"]["stale"] = 1
    mutations.append(integer_stale)
    boolean_numeric_value = deepcopy(payload)
    boolean_numeric_value["action"]["capability"]["value"] = True
    mutations.append(boolean_numeric_value)
    invalid_state_class = deepcopy(payload)
    invalid_state_class["action"]["capability"]["state_class"] = 1
    mutations.append(invalid_state_class)
    invalid_source = deepcopy(payload)
    invalid_source["action"]["capability"]["source"]["instance_id"] = " "
    mutations.append(invalid_source)
    unsafe_integer = deepcopy(payload)
    unsafe_integer["action"]["capability"]["value"] = MAX_SAFE_INTEGER + 1
    mutations.append(unsafe_integer)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_apply(selection, mutation)


@pytest.mark.parametrize("request_id", [None, True, "", "with space", "x" * 129])
def test_apply_codec_rejects_invalid_request_identifiers(
    request_id: object,
) -> None:
    """Correlation identifiers remain bounded and safe to log."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )

    with pytest.raises(
        ProtocolFormatError,
        match="^invalid protocol message$",
    ):
        build_apply(_selection(), request_id, action)

    selection = _selection()
    payload = build_apply(selection, "request-1", action)
    payload["request_id"] = request_id
    with pytest.raises(
        ProtocolFormatError,
        match="^invalid protocol message$",
    ):
        parse_apply(selection, payload)


def test_confirmed_apply_result_round_trips_exact_identity() -> None:
    """A confirmation binds its target to the action's source identity."""
    payload = build_apply_result(
        _selection(),
        "request-42",
        ApplyResultStatus.CONFIRMED,
        "123",
        _source_identity(),
    )

    assert payload == {
        "schema": 1,
        "type": "apply_result",
        "request_id": "request-42",
        "status": "confirmed",
        "target_id": "123",
        "source": {
            "system": "home_assistant",
            "instance_id": "ha-instance-1",
            "object_id": "sensor.living_room_temperature",
            "capability_id": "state",
        },
    }
    assert parse_apply_result(_selection(), payload) == ApplyResult(
        request_id="request-42",
        status=ApplyResultStatus.CONFIRMED,
        target_id="123",
        source=_source_identity(),
    )


def test_rejected_apply_result_has_no_remote_details() -> None:
    """A rejection carries only its correlation and sanitized status."""
    payload = build_apply_result(
        _selection(),
        "request-42",
        ApplyResultStatus.REJECTED,
        None,
        None,
    )

    assert payload == {
        "schema": 1,
        "type": "apply_result",
        "request_id": "request-42",
        "status": "rejected",
        "target_id": None,
        "source": None,
    }
    assert parse_apply_result(_selection(), payload) == ApplyResult(
        request_id="request-42",
        status=ApplyResultStatus.REJECTED,
        target_id=None,
        source=None,
    )


def test_apply_result_parser_rejects_extensions_and_malformed_results() -> None:
    """Results cannot add error details or contradict their status."""
    confirmed = build_apply_result(
        _selection(),
        "request-42",
        ApplyResultStatus.CONFIRMED,
        "123",
        _source_identity(),
    )
    rejected = build_apply_result(
        _selection(),
        "request-42",
        ApplyResultStatus.REJECTED,
        None,
        None,
    )
    mutations = []

    extra_result = deepcopy(rejected)
    extra_result["error"] = "remote detail"
    mutations.append(extra_result)
    missing_result_field = deepcopy(rejected)
    del missing_result_field["source"]
    mutations.append(missing_result_field)
    wrong_schema = deepcopy(rejected)
    wrong_schema["schema"] = 2
    mutations.append(wrong_schema)
    unknown_status = deepcopy(rejected)
    unknown_status["status"] = "failed"
    mutations.append(unknown_status)
    rejected_with_target = deepcopy(rejected)
    rejected_with_target["target_id"] = "123"
    mutations.append(rejected_with_target)
    rejected_with_source = deepcopy(rejected)
    rejected_with_source["source"] = confirmed["source"]
    mutations.append(rejected_with_source)
    confirmed_without_target = deepcopy(confirmed)
    confirmed_without_target["target_id"] = None
    mutations.append(confirmed_without_target)
    confirmed_without_source = deepcopy(confirmed)
    confirmed_without_source["source"] = None
    mutations.append(confirmed_without_source)
    source_extension = deepcopy(confirmed)
    source_extension["source"]["unexpected"] = True
    mutations.append(source_extension)
    whitespace_target = deepcopy(confirmed)
    whitespace_target["target_id"] = " 123"
    mutations.append(whitespace_target)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_apply_result(_selection(), mutation)


def test_apply_result_builder_requires_enum_and_consistent_fields() -> None:
    """Local callers cannot accidentally emit an ambiguous result."""
    invalid_arguments = [
        ("confirmed", "123", _source_identity()),
        (ApplyResultStatus.CONFIRMED, None, _source_identity()),
        (ApplyResultStatus.CONFIRMED, "123", None),
        (ApplyResultStatus.REJECTED, "123", None),
        (ApplyResultStatus.REJECTED, None, _source_identity()),
    ]

    for status, target_id, source in invalid_arguments:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            build_apply_result(
                _selection(),
                "request-42",
                status,
                target_id,
                source,
            )


def test_binary_codecs_require_the_independent_binary_feature() -> None:
    """Binary messages cannot ride on the numeric feature or vice versa."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_binary_capability(),
    )
    binary_selection = _selection((FEATURE_HA_EXPORT_BINARY_V1,))
    numeric_selection = _selection((FEATURE_HA_EXPORT_NUMERIC_V1,))
    binary_payload = build_binary_apply(binary_selection, "request-1", action)
    binary_result = build_binary_apply_result(
        binary_selection,
        "request-1",
        ApplyResultStatus.REJECTED,
        None,
        None,
    )

    with pytest.raises(ProtocolCompatibilityError):
        build_binary_apply(numeric_selection, "request-1", action)
    with pytest.raises(ProtocolCompatibilityError):
        parse_binary_apply(numeric_selection, binary_payload)
    with pytest.raises(ProtocolCompatibilityError):
        build_binary_apply_result(
            numeric_selection,
            "request-1",
            ApplyResultStatus.REJECTED,
            None,
            None,
        )
    with pytest.raises(ProtocolCompatibilityError):
        parse_binary_apply_result(numeric_selection, binary_result)

    numeric_action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    with pytest.raises(ProtocolCompatibilityError):
        build_apply(binary_selection, "request-2", numeric_action)


@pytest.mark.parametrize(
    "action",
    [
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=_binary_capability(),
        ),
        ReconciliationAction(
            kind=ReconciliationActionKind.UPDATE,
            capability=_binary_capability(value=False),
            target_id="42",
        ),
        ReconciliationAction(
            kind=ReconciliationActionKind.MARK_UNAVAILABLE,
            capability=_binary_capability(
                availability=Availability.UNAVAILABLE,
                value=None,
            ),
            target_id="42",
            stale=True,
        ),
    ],
)
def test_binary_apply_codec_round_trips_exact_actions(
    action: ReconciliationAction,
) -> None:
    """Every binary action uses its own exact schema-versioned message."""
    selection = _selection()
    payload = build_binary_apply(selection, "request-42", action)

    assert payload == {
        "schema": 1,
        "type": "binary_apply",
        "request_id": "request-42",
        "action": {
            "kind": action.kind.value,
            "capability": {
                "source": {
                    "system": "home_assistant",
                    "instance_id": "ha-instance-1",
                    "object_id": "binary_sensor.living_room_motion",
                    "capability_id": "state",
                },
                "kind": "binary",
                "name": "Living room motion",
                "value": action.capability.value,
                "availability": action.capability.availability.value,
                "semantic": "motion",
                "unit": None,
                "state_class": None,
            },
            "target_id": action.target_id,
            "stale": action.stale,
        },
    }
    assert parse_binary_apply(selection, payload) == ApplyRequest(
        request_id="request-42",
        action=action,
    )


@pytest.mark.parametrize("value", [0, 1, "on", "off"])
def test_binary_apply_parser_requires_real_boolean_values(value: object) -> None:
    """Binary availability cannot be confused with integers or text states."""
    selection = _selection()
    payload = build_binary_apply(
        selection,
        "request-1",
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=_binary_capability(),
        ),
    )
    payload["action"]["capability"]["value"] = value

    with pytest.raises(
        ProtocolFormatError,
        match="^invalid protocol message$",
    ):
        parse_binary_apply(selection, payload)


def test_binary_apply_parser_rejects_numeric_and_schema_extensions() -> None:
    """Binary requests reject the numeric kind and every unsigned extension."""
    selection = _selection()
    payload = build_binary_apply(
        selection,
        "request-1",
        ReconciliationAction(
            kind=ReconciliationActionKind.CREATE,
            capability=_binary_capability(),
        ),
    )
    mutations = []

    numeric_kind = deepcopy(payload)
    numeric_kind["action"]["capability"].update(
        kind="numeric",
        value=1,
        semantic=None,
        unit=None,
        state_class=None,
    )
    mutations.append(numeric_kind)
    extra_message = deepcopy(payload)
    extra_message["unexpected"] = True
    mutations.append(extra_message)
    missing_action_field = deepcopy(payload)
    del missing_action_field["action"]["stale"]
    mutations.append(missing_action_field)
    extra_capability_field = deepcopy(payload)
    extra_capability_field["action"]["capability"]["unexpected"] = True
    mutations.append(extra_capability_field)
    wrong_schema = deepcopy(payload)
    wrong_schema["schema"] = 2
    mutations.append(wrong_schema)
    numeric_discriminator = deepcopy(payload)
    numeric_discriminator["type"] = "apply"
    mutations.append(numeric_discriminator)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_binary_apply(selection, mutation)


def test_binary_apply_result_round_trips_confirmed_and_safe_rejected() -> None:
    """Binary results confirm identity or reject without remote error details."""
    selection = _selection()
    confirmed = build_binary_apply_result(
        selection,
        "request-42",
        ApplyResultStatus.CONFIRMED,
        "123",
        _binary_source_identity(),
    )
    rejected = build_binary_apply_result(
        selection,
        "request-43",
        ApplyResultStatus.REJECTED,
        None,
        None,
    )

    assert confirmed == {
        "schema": 1,
        "type": "binary_apply_result",
        "request_id": "request-42",
        "status": "confirmed",
        "target_id": "123",
        "source": {
            "system": "home_assistant",
            "instance_id": "ha-instance-1",
            "object_id": "binary_sensor.living_room_motion",
            "capability_id": "state",
        },
    }
    assert rejected == {
        "schema": 1,
        "type": "binary_apply_result",
        "request_id": "request-43",
        "status": "rejected",
        "target_id": None,
        "source": None,
    }
    assert parse_binary_apply_result(selection, confirmed) == ApplyResult(
        request_id="request-42",
        status=ApplyResultStatus.CONFIRMED,
        target_id="123",
        source=_binary_source_identity(),
    )
    assert parse_binary_apply_result(selection, rejected) == ApplyResult(
        request_id="request-43",
        status=ApplyResultStatus.REJECTED,
        target_id=None,
        source=None,
    )

    mutations = []
    rejected_with_details = deepcopy(rejected)
    rejected_with_details["error"] = "remote detail"
    mutations.append(rejected_with_details)
    missing_result_field = deepcopy(rejected)
    del missing_result_field["source"]
    mutations.append(missing_result_field)
    wrong_schema = deepcopy(rejected)
    wrong_schema["schema"] = 2
    mutations.append(wrong_schema)
    numeric_discriminator = deepcopy(rejected)
    numeric_discriminator["type"] = "apply_result"
    mutations.append(numeric_discriminator)
    rejected_with_target = deepcopy(rejected)
    rejected_with_target["target_id"] = "123"
    mutations.append(rejected_with_target)
    rejected_with_source = deepcopy(rejected)
    rejected_with_source["source"] = confirmed["source"]
    mutations.append(rejected_with_source)
    confirmed_without_target = deepcopy(confirmed)
    confirmed_without_target["target_id"] = None
    mutations.append(confirmed_without_target)
    confirmed_without_source = deepcopy(confirmed)
    confirmed_without_source["source"] = None
    mutations.append(confirmed_without_source)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_binary_apply_result(selection, mutation)


@pytest.mark.parametrize(
    ("status", "target_id", "source"),
    [
        ("confirmed", "123", _binary_source_identity()),
        (ApplyResultStatus.CONFIRMED, None, _binary_source_identity()),
        (ApplyResultStatus.CONFIRMED, "123", None),
        (ApplyResultStatus.REJECTED, "123", None),
        (ApplyResultStatus.REJECTED, None, _binary_source_identity()),
    ],
)
def test_binary_apply_result_builder_rejects_ambiguous_results(
    status: object,
    target_id: object,
    source: object,
) -> None:
    """Local binary result callers cannot emit contradictory fields."""
    with pytest.raises(
        ProtocolFormatError,
        match="^invalid protocol message$",
    ):
        build_binary_apply_result(
            _selection(),
            "request-42",
            status,
            target_id,
            source,
        )
