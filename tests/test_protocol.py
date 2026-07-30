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
    MAX_MESSAGE_BYTES,
    MAX_SAFE_INTEGER,
    PROTOCOL_VERSION,
    ApplyRequest,
    ApplyResult,
    ApplyResultStatus,
    ClientHello,
    HandshakeContext,
    ProtocolAuthenticationError,
    ProtocolFormatError,
    ProtocolSequenceError,
    accept_challenge,
    build_apply,
    build_apply_result,
    build_authenticate,
    build_challenge,
    build_hello,
    build_ready,
    canonical_json_bytes,
    canonical_json_dumps,
    canonical_json_loads,
    create_client_proof,
    create_server_proof,
    derive_session_id,
    derive_session_key,
    generate_destination_id,
    generate_link_id,
    generate_nonce,
    generate_pairing_key,
    generate_request_id,
    make_handshake_context,
    parse_apply,
    parse_apply_result,
    parse_hello,
    sign_envelope,
    validate_destination_id,
    validate_link_id,
    validate_nonce,
    validate_pairing_key,
    verify_authenticate,
    verify_client_proof,
    verify_envelope,
    verify_ready,
    verify_server_proof,
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
) -> Capability:
    return Capability(
        source=_source_identity(),
        kind=CapabilityKind.NUMERIC,
        name="Living room temperature",
        value=value,
        availability=availability,
        semantic="temperature",
        unit="\N{DEGREE SIGN}C",
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
    assert build_apply(request_id, action)["request_id"] == request_id


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


def test_signed_envelope_round_trip_and_defensive_payload_copy() -> None:
    """A valid canonical frame returns its authenticated application fields."""
    session_key, _, session_id = _session()
    payload: dict[str, object] = {
        "type": "ping",
        "details": {"counter": 1},
    }
    envelope = sign_envelope(
        session_key,
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
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session_id,
            last_sequence=0,
        )


def test_envelope_rejects_wrong_session_key_direction_or_session() -> None:
    """An authenticated frame is accepted only in its intended channel."""
    session_key, _, session_id = _session()
    envelope = sign_envelope(
        session_key,
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
                expected_direction=DIRECTION_DOMOTICZ_TO_HA,
                expected_session_id=session_id,
                last_sequence=0,
            )

    with pytest.raises(ProtocolFormatError):
        sign_envelope(
            session_key,
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
        direction=DIRECTION_DOMOTICZ_TO_HA,
        session_id=session_id,
        sequence=1,
        payload={"type": "ping"},
    )
    verify_envelope(
        session_key,
        envelope,
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
    payload = build_apply("request-42", action)

    assert payload == {
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
            },
            "target_id": action.target_id,
            "stale": action.stale,
        },
    }
    assert parse_apply(payload) == ApplyRequest(
        request_id="request-42",
        action=action,
    )


def test_apply_payload_can_be_signed_verified_and_parsed() -> None:
    """The strict request codec composes with authenticated envelopes."""
    session_key, _, session_id = _session()
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    envelope = sign_envelope(
        session_key,
        direction=DIRECTION_HA_TO_DOMOTICZ,
        session_id=session_id,
        sequence=1,
        payload=build_apply("request-1", action),
    )

    verified = verify_envelope(
        session_key,
        envelope,
        expected_direction=DIRECTION_HA_TO_DOMOTICZ,
        expected_session_id=session_id,
        last_sequence=0,
    )

    assert parse_apply(verified.payload).action == action


def test_apply_parser_rejects_extra_or_missing_fields_at_every_level() -> None:
    """No unsigned extension point exists in an apply request."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    payload = build_apply("request-1", action)
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
    missing_action_field = deepcopy(payload)
    del missing_action_field["action"]["stale"]
    mutations.append(missing_action_field)
    missing_capability_field = deepcopy(payload)
    del missing_capability_field["action"]["capability"]["semantic"]
    mutations.append(missing_capability_field)
    missing_source_field = deepcopy(payload)
    del missing_source_field["action"]["capability"]["source"]["instance_id"]
    mutations.append(missing_source_field)

    for mutation in mutations:
        with pytest.raises(
            ProtocolFormatError,
            match="^invalid protocol message$",
        ):
            parse_apply(mutation)


def test_apply_parser_rejects_malformed_action_semantics() -> None:
    """Wire input cannot bypass the neutral reconciliation model."""
    action = ReconciliationAction(
        kind=ReconciliationActionKind.CREATE,
        capability=_numeric_capability(),
    )
    payload = build_apply("request-1", action)
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
            parse_apply(mutation)


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
        build_apply(request_id, action)

    payload = build_apply("request-1", action)
    payload["request_id"] = request_id
    with pytest.raises(
        ProtocolFormatError,
        match="^invalid protocol message$",
    ):
        parse_apply(payload)


def test_confirmed_apply_result_round_trips_exact_identity() -> None:
    """A confirmation binds its target to the action's source identity."""
    payload = build_apply_result(
        "request-42",
        ApplyResultStatus.CONFIRMED,
        "123",
        _source_identity(),
    )

    assert payload == {
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
    assert parse_apply_result(payload) == ApplyResult(
        request_id="request-42",
        status=ApplyResultStatus.CONFIRMED,
        target_id="123",
        source=_source_identity(),
    )


def test_rejected_apply_result_has_no_remote_details() -> None:
    """A rejection carries only its correlation and sanitized status."""
    payload = build_apply_result(
        "request-42",
        ApplyResultStatus.REJECTED,
        None,
        None,
    )

    assert payload == {
        "type": "apply_result",
        "request_id": "request-42",
        "status": "rejected",
        "target_id": None,
        "source": None,
    }
    assert parse_apply_result(payload) == ApplyResult(
        request_id="request-42",
        status=ApplyResultStatus.REJECTED,
        target_id=None,
        source=None,
    )


def test_apply_result_parser_rejects_extensions_and_malformed_results() -> None:
    """Results cannot add error details or contradict their status."""
    confirmed = build_apply_result(
        "request-42",
        ApplyResultStatus.CONFIRMED,
        "123",
        _source_identity(),
    )
    rejected = build_apply_result(
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
            parse_apply_result(mutation)


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
                "request-42",
                status,
                target_id,
                source,
            )
