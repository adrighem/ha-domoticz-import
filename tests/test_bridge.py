"""Tests for the authenticated Domoticz companion bridge endpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from aiohttp import WSCloseCode, WSMsgType  # noqa: E402
from aiohttp.test_utils import TestClient  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.typing import (  # noqa: E402
    ClientSessionGenerator,
)

from custom_components.domoticz_sync import bridge as bridge_module  # noqa: E402
from custom_components.domoticz_sync.bridge import (  # noqa: E402
    BRIDGE_WEBSOCKET_PATH,
    MAX_BRIDGE_MESSAGE_BYTES,
    MAX_PENDING_HANDSHAKES,
    DomoticzBridgeManager,
    DomoticzBridgeView,
)
from custom_components.domoticz_sync.core.protocol import (  # noqa: E402
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
    accept_challenge,
    build_authenticate,
    build_hello,
    canonical_json_dumps,
    canonical_json_loads,
    derive_session_key,
    generate_destination_id,
    generate_link_id,
    generate_nonce,
    generate_pairing_key,
    parse_hello,
    sign_envelope,
    verify_envelope,
    verify_ready,
)


@dataclass
class AuthenticatedClient:
    """State needed to exchange signed application messages in tests."""

    websocket: object
    session_key: bytes
    session_id: str
    client_sequence: int = 1
    server_sequence: int = 1

    async def async_send(self, payload: dict[str, object]) -> None:
        """Send the next signed client payload."""
        self.client_sequence += 1
        await self.websocket.send_str(
            canonical_json_dumps(
                sign_envelope(
                    self.session_key,
                    direction=DIRECTION_DOMOTICZ_TO_HA,
                    session_id=self.session_id,
                    sequence=self.client_sequence,
                    payload=payload,
                )
            )
        )

    async def async_receive(self) -> dict[str, object]:
        """Receive and verify the next signed server payload."""
        document = canonical_json_loads(await self.websocket.receive_str())
        verified = verify_envelope(
            self.session_key,
            document,
            expected_direction=DIRECTION_HA_TO_DOMOTICZ,
            expected_session_id=self.session_id,
            last_sequence=self.server_sequence,
        )
        self.server_sequence = verified.sequence
        return verified.payload


async def _async_create_endpoint(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    manager: DomoticzBridgeManager,
) -> TestClient:
    """Register the singleton test endpoint and return an HTTP client."""
    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    hass.http.register_view(DomoticzBridgeView(manager))
    return await hass_client_no_auth()


async def _async_start_handshake(
    client: TestClient,
    *,
    link_id: str,
    pairing_key: str,
    destination_id: str | None = None,
):
    """Start and authenticate one bridge WebSocket."""
    websocket = await client.ws_connect(BRIDGE_WEBSOCKET_PATH, compress=15)
    hello_document = build_hello(
        link_id,
        destination_id or generate_destination_id(),
        generate_nonce(),
    )
    hello = parse_hello(hello_document)
    await websocket.send_str(canonical_json_dumps(hello_document))

    challenge = canonical_json_loads(await websocket.receive_str())
    context = accept_challenge(pairing_key, hello, challenge)
    await websocket.send_str(
        canonical_json_dumps(build_authenticate(pairing_key, context))
    )
    session_key = derive_session_key(pairing_key, context)
    return websocket, context, session_key


async def _async_connect(
    client: TestClient,
    *,
    link_id: str,
    pairing_key: str,
    destination_id: str | None = None,
) -> AuthenticatedClient:
    """Complete mutual authentication and the empty-inventory exchange."""
    websocket, context, session_key = await _async_start_handshake(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
        destination_id=destination_id,
    )
    ready = canonical_json_loads(await websocket.receive_str())
    session_id = verify_ready(session_key, context, ready)

    await websocket.send_str(
        canonical_json_dumps(
            sign_envelope(
                session_key,
                direction=DIRECTION_DOMOTICZ_TO_HA,
                session_id=session_id,
                sequence=1,
                payload={"targets": [], "type": "inventory"},
            )
        )
    )
    server_ready = canonical_json_loads(await websocket.receive_str())
    verified = verify_envelope(
        session_key,
        server_ready,
        expected_direction=DIRECTION_HA_TO_DOMOTICZ,
        expected_session_id=session_id,
        last_sequence=0,
    )
    assert verified.payload == {"type": "ready"}
    return AuthenticatedClient(websocket, session_key, session_id)


@pytest.mark.asyncio
async def test_mutual_auth_inventory_and_signed_ping(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A configured plugin reaches ready and exchanges signed heartbeat traffic."""
    manager = DomoticzBridgeManager()
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="entry-1",
        link_id=link_id,
        pairing_key=pairing_key,
    )
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)

    connection = await _async_connect(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert connection.websocket.compress == 0
    assert await manager.async_is_ready(link_id)

    ping_id = generate_nonce()
    await connection.async_send({"id": ping_id, "type": "ping"})
    assert await connection.async_receive() == {"id": ping_id, "type": "pong"}

    await connection.websocket.close()
    for _ in range(10):
        if await manager.async_active_session_count() == 0:
            break
        await asyncio.sleep(0)
    assert await manager.async_active_session_count() == 0


@pytest.mark.asyncio
async def test_duplicate_active_session_is_rejected(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Only one authenticated connection may own a configured link."""
    manager = DomoticzBridgeManager()
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="entry-1",
        link_id=link_id,
        pairing_key=pairing_key,
    )
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)
    first = await _async_connect(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    duplicate, _context, _session_key = await _async_start_handshake(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    close = await duplicate.receive()

    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert duplicate.close_code == WSCloseCode.POLICY_VIOLATION
    assert await manager.async_is_ready(link_id)
    await duplicate.close()
    await first.websocket.close()


@pytest.mark.asyncio
async def test_failed_ready_send_releases_claim_and_allows_reconnect(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed final handshake send cannot leave a stale claimed session."""
    manager = DomoticzBridgeManager()
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="entry-1",
        link_id=link_id,
        pairing_key=pairing_key,
    )
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)
    original_send = bridge_module._async_send_document
    fail_ready_once = True

    async def _async_fail_first_ready(websocket, document):
        nonlocal fail_ready_once
        if (
            fail_ready_once
            and isinstance(document, dict)
            and document.get("type") == "ready"
        ):
            fail_ready_once = False
            raise ConnectionResetError
        await original_send(websocket, document)

    monkeypatch.setattr(
        bridge_module,
        "_async_send_document",
        _async_fail_first_ready,
    )

    failed, _context, _session_key = await _async_start_handshake(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    close = await failed.receive()
    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert await manager.async_active_session_count() == 0

    reconnected = await _async_connect(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    assert await manager.async_is_ready(link_id)
    await failed.close()
    await reconnected.websocket.close()


@pytest.mark.asyncio
async def test_unknown_link_is_rejected_before_challenge(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """An unknown high-entropy link cannot occupy a handshake slot."""
    manager = DomoticzBridgeManager()
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)
    websocket = await client.ws_connect(BRIDGE_WEBSOCKET_PATH)
    hello_document = build_hello(
        generate_link_id(),
        generate_destination_id(),
        generate_nonce(),
    )
    await websocket.send_str(canonical_json_dumps(hello_document))

    close = await websocket.receive()

    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert websocket.close_code == WSCloseCode.POLICY_VIOLATION
    await websocket.close()
    assert await manager.async_active_session_count() == 0


@pytest.mark.asyncio
async def test_non_empty_inventory_is_rejected_for_connection_spike(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The connection-only spike cannot smuggle target operations."""
    manager = DomoticzBridgeManager()
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="entry-1",
        link_id=link_id,
        pairing_key=pairing_key,
    )
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)
    websocket, context, session_key = await _async_start_handshake(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    ready = canonical_json_loads(await websocket.receive_str())
    session_id = verify_ready(session_key, context, ready)
    await websocket.send_str(
        canonical_json_dumps(
            sign_envelope(
                session_key,
                direction=DIRECTION_DOMOTICZ_TO_HA,
                session_id=session_id,
                sequence=1,
                payload={
                    "targets": [{"target_id": "not-accepted"}],
                    "type": "inventory",
                },
            )
        )
    )

    close = await websocket.receive()

    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert websocket.close_code == WSCloseCode.POLICY_VIOLATION
    assert not await manager.async_is_ready(link_id)
    await websocket.close()


@pytest.mark.asyncio
async def test_unregister_closes_session_and_disables_link(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Unloading an entry closes its connection and removes its credential."""
    manager = DomoticzBridgeManager()
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="entry-1",
        link_id=link_id,
        pairing_key=pairing_key,
    )
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)
    connection = await _async_connect(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    await manager.async_unregister_entry("entry-1")
    close = await connection.websocket.receive()

    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert connection.websocket.close_code == WSCloseCode.GOING_AWAY
    assert not await manager.async_is_ready(link_id)
    await connection.websocket.close()


@pytest.mark.asyncio
async def test_pre_authentication_connections_are_bounded() -> None:
    """Unauthenticated clients cannot consume unbounded handshake slots."""
    manager = DomoticzBridgeManager()

    assert all(
        [await manager.async_reserve_handshake() for _ in range(MAX_PENDING_HANDSHAKES)]
    )
    assert not await manager.async_reserve_handshake()

    for _ in range(MAX_PENDING_HANDSHAKES):
        await manager.async_release_handshake()
    assert await manager.async_reserve_handshake()
    await manager.async_release_handshake()


@pytest.mark.asyncio
async def test_authentication_timeout_closes_connection(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent unauthenticated peer releases its bounded handshake slot."""
    monkeypatch.setattr(bridge_module, "FIRST_MESSAGE_TIMEOUT", 0.01)
    manager = DomoticzBridgeManager()
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)

    websocket = await client.ws_connect(BRIDGE_WEBSOCKET_PATH)
    close = await websocket.receive()

    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert websocket.close_code == WSCloseCode.POLICY_VIOLATION
    assert await manager.async_active_session_count() == 0
    await websocket.close()


@pytest.mark.asyncio
async def test_server_pong_timeout_is_absolute_while_client_pings_interleave(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid client pings cannot postpone the deadline for the server's pong."""
    monkeypatch.setattr(bridge_module, "HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(bridge_module, "HEARTBEAT_RESPONSE_TIMEOUT", 0.3)
    manager = DomoticzBridgeManager()
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="entry-1",
        link_id=link_id,
        pairing_key=pairing_key,
    )
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)
    connection = await _async_connect(
        client,
        link_id=link_id,
        pairing_key=pairing_key,
    )

    server_ping = await connection.async_receive()
    assert server_ping["type"] == "ping"
    started = asyncio.get_running_loop().time()

    for _ in range(2):
        await asyncio.sleep(0.08)
        client_ping_id = generate_nonce()
        await connection.async_send({"id": client_ping_id, "type": "ping"})
        assert await connection.async_receive() == {
            "id": client_ping_id,
            "type": "pong",
        }

    async with asyncio.timeout(0.2):
        close = await connection.websocket.receive()

    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert connection.websocket.close_code == WSCloseCode.GOING_AWAY
    assert asyncio.get_running_loop().time() - started < 0.38
    for _ in range(10):
        if not await manager.async_is_ready(link_id):
            break
        await asyncio.sleep(0)
    assert not await manager.async_is_ready(link_id)
    await connection.websocket.close()


@pytest.mark.asyncio
async def test_oversized_message_is_rejected(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """The WebSocket layer rejects an assembled message above the hard limit."""
    manager = DomoticzBridgeManager()
    client = await _async_create_endpoint(hass, hass_client_no_auth, manager)
    websocket = await client.ws_connect(BRIDGE_WEBSOCKET_PATH)

    await websocket.send_str("x" * (MAX_BRIDGE_MESSAGE_BYTES + 1))
    close = await websocket.receive()

    assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
    assert websocket.close_code == WSCloseCode.MESSAGE_TOO_BIG
    assert await manager.async_active_session_count() == 0
    await websocket.close()
