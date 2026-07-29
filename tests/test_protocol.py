"""Tests for the host-neutral authenticated connection protocol."""

from __future__ import annotations

import base64
import math
import re
from copy import deepcopy

import pytest

from custom_components.domoticz_sync.core import protocol
from custom_components.domoticz_sync.core.protocol import (
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
    MAX_SAFE_INTEGER,
    PROTOCOL_VERSION,
    ClientHello,
    HandshakeContext,
    ProtocolAuthenticationError,
    ProtocolFormatError,
    ProtocolSequenceError,
    accept_challenge,
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
    make_handshake_context,
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

_URLSAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


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
