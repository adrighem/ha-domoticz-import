"""Authenticated Home Assistant endpoint for the Domoticz companion plugin."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Final, Protocol

from aiohttp import WSCloseCode, WSMsgType, web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .catalog_storage import HomeAssistantCatalogStorage, HomeAssistantBinaryCatalogStorage
from .core import catalog_from_document, Capability
from .core.protocol import (
    DIRECTION_DOMOTICZ_TO_HA,
    DIRECTION_HA_TO_DOMOTICZ,
    FEATURE_DOMOTICZ_CONTROL_V1,
    FEATURE_HA_EXPORT_BINARY_V1,
    FEATURE_HA_EXPORT_CONTINUOUS_V1,
    FEATURE_HA_EXPORT_NUMERIC_V1,
    MAX_INVENTORY_PAGES,
    PROTOCOL_VERSION,
    SUPPORTED_V2_FEATURES,
    SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
    ControlResultStatus,
    ProtocolAuthenticationError,
    ProtocolCompatibilityError,
    ProtocolError,
    ProtocolSelection,
    build_application_ready,
    build_challenge,
    build_control_result,
    build_ready,
    build_v2_challenge,
    build_v2_ready,
    canonical_json_dumps,
    canonical_json_loads,
    derive_session_id,
    derive_session_key,
    derive_v2_session_id,
    derive_v2_session_key,
    generate_nonce,
    make_handshake_context,
    make_v2_handshake_context,
    parse_application_ready,
    parse_control,
    parse_hello,
    parse_v2_hello,
    select_websocket_subprotocol,
    sign_envelope,
    validate_link_id,
    validate_nonce,
    validate_pairing_key,
    validate_protocol_tokens,
    verify_authenticate,
    verify_envelope,
    verify_v2_authenticate,
)

BRIDGE_WEBSOCKET_PATH: Final = "/api/domoticz_sync/websocket"

MAX_BRIDGE_MESSAGE_BYTES: Final = 64 * 1024
MAX_APPLICATION_INBOX_MESSAGES: Final = MAX_INVENTORY_PAGES
MAX_PENDING_HANDSHAKES: Final = 8
PREPARE_TIMEOUT: Final = 5.0
FIRST_MESSAGE_TIMEOUT: Final = 3.0
AUTHENTICATION_TIMEOUT: Final = 10.0
INVENTORY_TIMEOUT: Final = 10.0
HEARTBEAT_INTERVAL: Final = 30.0
HEARTBEAT_RESPONSE_TIMEOUT: Final = 10.0

_POLICY_CLOSE_MESSAGE: Final = b"Protocol error"
_PEER_CLOSE_MESSAGE: Final = b"Connection closed"
_SHUTDOWN_CLOSE_MESSAGE: Final = b"Bridge unavailable"


class BridgeConfigurationError(ValueError):
    """A bridge link conflicts with another configured link."""


class _PeerClosed(Exception):
    """The peer closed its connection normally."""


class BridgeApplication(Protocol):
    """Application invoked for one ready, authenticated bridge session."""

    async def async_connected(self, session: BridgeApplicationSession) -> None:
        """Use one ready application session until its transport ends."""


@dataclass(frozen=True, slots=True)
class BridgeLink:
    """Credentials for one configured Domoticz bridge."""

    entry_id: str
    link_id: str
    pairing_key: str = field(repr=False)

    def __post_init__(self) -> None:
        """Validate credentials even when constructed directly."""
        validate_link_id(self.link_id)
        validate_pairing_key(self.pairing_key)


@dataclass(slots=True)
class BridgeSession:
    """One mutually authenticated Domoticz connection."""

    entry_id: str
    link_id: str
    destination_id: str
    session_id: str
    websocket: web.WebSocketResponse = field(repr=False)
    session_key: bytes = field(repr=False)
    selection: ProtocolSelection | None = None
    client_sequence: int = 0
    server_sequence: int = 0
    ready: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    application_session: BridgeApplicationSession | None = field(
        default=None,
        repr=False,
    )
    application_task: asyncio.Task[None] | None = field(default=None, repr=False)


class BridgeApplicationSession:
    """Narrow signed-payload facade for a ready bridge session."""

    __slots__ = ("_active", "_deactivated", "_inbox", "_manager", "_session")

    def __init__(
        self,
        manager: DomoticzBridgeManager,
        session: BridgeSession,
    ) -> None:
        """Bind the facade to one active bridge session."""
        self._manager = manager
        self._session = session
        self._active = True
        self._deactivated = asyncio.Event()
        self._inbox: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=MAX_APPLICATION_INBOX_MESSAGES
        )

    @property
    def entry_id(self) -> str:
        """Return the Home Assistant config entry owning this session."""
        return self._session.entry_id

    @property
    def destination_id(self) -> str:
        """Return the authenticated Domoticz destination identifier."""
        return self._session.destination_id

    @property
    def selection(self) -> ProtocolSelection:
        """Return the authenticated protocol and feature selection."""
        selection = self._session.selection
        if selection is None:
            raise ProtocolError("application session is unavailable")
        return selection

    def supports(self, feature: str) -> bool:
        """Return whether the session negotiated one optional feature."""
        return self.selection.supports(feature)

    async def async_send(self, payload: dict[str, object]) -> None:
        """Send one signed, in-order application payload."""
        self._ensure_active()
        await self._manager._async_send_payload(self._session, payload)

    async def async_receive(self) -> dict[str, object]:
        """Receive one signed, in-order application payload."""
        self._ensure_active()
        payload_task = asyncio.create_task(self._inbox.get())
        deactivation_task = asyncio.create_task(self._deactivated.wait())
        try:
            await asyncio.wait(
                (payload_task, deactivation_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            self._ensure_active()
            return payload_task.result()
        finally:
            for task in (payload_task, deactivation_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                payload_task,
                deactivation_task,
                return_exceptions=True,
            )

    def _deliver(self, payload: dict[str, object]) -> None:
        """Route one manager-verified payload into the bounded application inbox."""
        self._ensure_active()
        try:
            self._inbox.put_nowait(payload)
        except asyncio.QueueFull:
            raise ProtocolError("application session is unavailable") from None

    def _deactivate(self) -> None:
        """Prevent application traffic after the session detaches."""
        if not self._active:
            return
        self._active = False
        self._deactivated.set()
        while True:
            try:
                self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _ensure_active(self) -> None:
        """Reject use after the application session detaches."""
        if not self._active:
            raise ProtocolError("application session is no longer active")


class DomoticzBridgeManager:
    """Own configured links and their active authenticated sessions."""

    def __init__(self, application: BridgeApplication | None = None) -> None:
        """Initialize an empty manager."""
        self._application = application
        self._lock = asyncio.Lock()
        self._links: dict[str, BridgeLink] = {}
        self._entry_links: dict[str, str] = {}
        self._sessions: dict[str, BridgeSession] = {}
        self._pending_handshakes = 0

    async def async_register_link(
        self,
        *,
        entry_id: str,
        link_id: str,
        pairing_key: str,
    ) -> None:
        """Register or atomically replace one config entry's bridge link."""
        replacement = BridgeLink(entry_id, link_id, pairing_key)
        session_to_close: BridgeSession | None = None

        async with self._lock:
            owner = self._links.get(link_id)
            if owner is not None and owner.entry_id != entry_id:
                raise BridgeConfigurationError("bridge link is already configured")

            previous_link_id = self._entry_links.get(entry_id)
            if previous_link_id is not None and previous_link_id != link_id:
                self._links.pop(previous_link_id, None)
                session_to_close = self._sessions.pop(previous_link_id, None)
                if session_to_close is not None:
                    self._deactivate_session(session_to_close)

            current_session = self._sessions.get(link_id)
            if (
                current_session is not None
                and owner is not None
                and owner.pairing_key != pairing_key
            ):
                session_to_close = self._sessions.pop(link_id)
                self._deactivate_session(session_to_close)

            self._links[link_id] = replacement
            self._entry_links[entry_id] = link_id

        if session_to_close is not None:
            await self._async_close_session(
                session_to_close,
                WSCloseCode.GOING_AWAY,
                _SHUTDOWN_CLOSE_MESSAGE,
            )

    async def async_unregister_entry(self, entry_id: str) -> None:
        """Remove a config entry and close its active bridge connection."""
        async with self._lock:
            link_id = self._entry_links.pop(entry_id, None)
            if link_id is None:
                return
            self._links.pop(link_id, None)
            session = self._sessions.pop(link_id, None)
            if session is not None:
                self._deactivate_session(session)

        if session is not None:
            await self._async_close_session(
                session,
                WSCloseCode.GOING_AWAY,
                _SHUTDOWN_CLOSE_MESSAGE,
            )

    async def async_shutdown(self) -> None:
        """Close all active sessions during Home Assistant shutdown."""
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            self._links.clear()
            self._entry_links.clear()
            for session in sessions:
                self._deactivate_session(session)

        await asyncio.gather(
            *(
                self._async_close_session(
                    session,
                    WSCloseCode.GOING_AWAY,
                    _SHUTDOWN_CLOSE_MESSAGE,
                )
                for session in sessions
            )
        )

    async def async_reserve_handshake(self) -> bool:
        """Reserve one bounded unauthenticated handshake slot."""
        async with self._lock:
            if self._pending_handshakes >= MAX_PENDING_HANDSHAKES:
                return False
            self._pending_handshakes += 1
            return True

    async def async_release_handshake(self) -> None:
        """Release a previously reserved handshake slot."""
        async with self._lock:
            if self._pending_handshakes > 0:
                self._pending_handshakes -= 1

    async def async_handle_reserved(
        self,
        websocket: web.WebSocketResponse,
        *,
        client_protocols: tuple[str, ...],
        selected_protocol: str | None,
    ) -> None:
        """Authenticate and run a connection with a reserved handshake slot."""
        session: BridgeSession | None = None
        reservation_released = False

        try:
            async with asyncio.timeout(AUTHENTICATION_TIMEOUT):
                session = await self._async_authenticate(
                    websocket,
                    client_protocols=client_protocols,
                    selected_protocol=selected_protocol,
                )

            await self.async_release_handshake()
            reservation_released = True
            try:
                await self._async_run_session(session)
            except Exception as error:
                self._raise_normalized_session_error(error)
        except _PeerClosed:
            await _async_close(
                websocket,
                WSCloseCode.GOING_AWAY,
                _PEER_CLOSE_MESSAGE,
            )
        except ProtocolError, TimeoutError:
            await _async_close(
                websocket,
                WSCloseCode.POLICY_VIOLATION,
                _POLICY_CLOSE_MESSAGE,
            )
        except ConnectionError:
            # aiohttp may surface a transport loss while sending or receiving.
            await _async_close(
                websocket,
                WSCloseCode.GOING_AWAY,
                _PEER_CLOSE_MESSAGE,
            )
        finally:
            if not reservation_released:
                await self.async_release_handshake()
            if session is not None:
                await self._async_release_session(session)

    async def async_is_ready(self, link_id: str) -> bool:
        """Return whether a link currently has a ready authenticated session."""
        async with self._lock:
            session = self._sessions.get(link_id)
            return session is not None and session.ready

    async def async_active_session_count(self) -> int:
        """Return the number of authenticated sessions."""
        async with self._lock:
            return len(self._sessions)

    async def _async_authenticate(
        self,
        websocket: web.WebSocketResponse,
        *,
        client_protocols: tuple[str, ...],
        selected_protocol: str | None,
    ) -> BridgeSession:
        """Perform mutual authentication and claim the configured link."""
        async with asyncio.timeout(FIRST_MESSAGE_TIMEOUT):
            hello_document = await _async_receive_document(websocket)

        selection: ProtocolSelection | None = None
        if selected_protocol is None:
            if client_protocols:
                raise ProtocolCompatibilityError("incompatible protocol")
            hello = parse_hello(hello_document)
        else:
            if (
                select_websocket_subprotocol(
                    client_protocols,
                    SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
                )
                != selected_protocol
            ):
                raise ProtocolCompatibilityError("incompatible protocol")
            hello = parse_v2_hello(hello_document)
            if (
                hello.client_protocols != client_protocols
                or hello.selected_protocol != selected_protocol
            ):
                raise ProtocolAuthenticationError("protocol authentication failed")

        async with self._lock:
            link = self._links.get(hello.link_id)
        # Link IDs carry 128 bits of randomness and are not authentication
        # credentials. Rejecting an unknown ID here releases scarce pre-auth
        # capacity without weakening pairing-key authentication.
        if link is None:
            raise ProtocolAuthenticationError("protocol authentication failed")
        pairing_key = link.pairing_key

        if selected_protocol is None:
            context = make_handshake_context(hello, generate_nonce())
            await _async_send_document(
                websocket,
                build_challenge(pairing_key, context),
            )
            verify_authenticate(
                pairing_key,
                context,
                await _async_receive_document(websocket),
            )
            session_key = derive_session_key(pairing_key, context)
            session_id = derive_session_id(session_key, context)
            ready_document = build_ready(session_key, context)
        else:
            context_v2 = make_v2_handshake_context(
                hello,
                generate_nonce(),
                server_protocols=SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
                server_features=SUPPORTED_V2_FEATURES,
            )
            await _async_send_document(
                websocket,
                build_v2_challenge(pairing_key, context_v2),
            )
            verify_v2_authenticate(
                pairing_key,
                context_v2,
                await _async_receive_document(websocket),
            )
            session_key = derive_v2_session_key(pairing_key, context_v2)
            session_id = derive_v2_session_id(session_key, context_v2)
            selection = context_v2.selection
            ready_document = build_v2_ready(session_key, context_v2)

        session = BridgeSession(
            entry_id=link.entry_id,
            link_id=link.link_id,
            destination_id=hello.destination_id,
            session_id=session_id,
            websocket=websocket,
            session_key=session_key,
            selection=selection,
        )

        async with self._lock:
            if session.link_id in self._sessions:
                raise ProtocolError("protocol authentication failed")
            # Re-check after the network round trip so an unload or rotation
            # cannot authenticate against stale credentials.
            if self._links.get(session.link_id) != link:
                raise ProtocolError("protocol authentication failed")
            self._sessions[session.link_id] = session

        try:
            await _async_send_document(websocket, ready_document)
        except BaseException:
            await self._async_release_session(session)
            raise
        return session

    async def _async_run_session(self, session: BridgeSession) -> None:
        """Complete the selected application startup and keep the link alive."""
        if session.selection is None:
            await self._async_run_legacy_session(session)
        else:
            await self._async_run_v2_session(session)

    async def _async_run_legacy_session(self, session: BridgeSession) -> None:
        """Preserve the released v1 inventory, ready, and heartbeat behavior."""
        async with asyncio.timeout(INVENTORY_TIMEOUT):
            inventory = await self._async_receive_payload(session)
        if inventory != {"targets": [], "type": "inventory"}:
            raise ProtocolError("invalid protocol message")

        await self._async_send_payload(session, {"type": "ready"})
        await self._async_mark_ready(session)

        await self._async_run_heartbeat(session)

    async def _async_run_v2_session(self, session: BridgeSession) -> None:
        """Exchange application readiness and run negotiated v2 behavior."""
        selection = session.selection
        assert selection is not None

        async with asyncio.timeout(INVENTORY_TIMEOUT):
            parse_application_ready(
                selection,
                await self._async_receive_payload(session),
            )
        await self._async_send_payload(
            session,
            build_application_ready(selection),
        )
        await self._async_mark_ready(session)

        continuous_enabled = selection.supports(FEATURE_HA_EXPORT_CONTINUOUS_V1)
        if continuous_enabled and self._application is None:
            raise ProtocolError("application session is unavailable")
        if self._application is not None and any(
            selection.supports(feature)
            for feature in (
                FEATURE_HA_EXPORT_NUMERIC_V1,
                FEATURE_HA_EXPORT_BINARY_V1,
                FEATURE_HA_EXPORT_CONTINUOUS_V1,
            )
        ):
            await self._async_run_application(session)
            return

        await self._async_run_heartbeat(session, None)

    async def _async_run_application(self, session: BridgeSession) -> None:
        """Run one application task beside the session's sole socket reader."""
        application = self._application
        assert application is not None
        application_session = BridgeApplicationSession(self, session)
        session.application_session = application_session
        application_task = asyncio.create_task(
            application.async_connected(application_session)
        )
        session.application_task = application_task
        reader_task = asyncio.create_task(
            self._async_run_heartbeat(session, application_session)
        )
        primary_error: BaseException | None = None
        try:
            done, _pending = await asyncio.wait(
                (application_task, reader_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if application_task in done:
                try:
                    await application_task
                except (asyncio.CancelledError, Exception) as error:
                    if isinstance(error, asyncio.CancelledError) and not session.ready:
                        try:
                            await reader_task
                        except (asyncio.CancelledError, Exception) as reader_error:
                            primary_error = reader_error
                    else:
                        primary_error = error
                else:
                    application_session._deactivate()
                    try:
                        await reader_task
                    except (asyncio.CancelledError, Exception) as error:
                        primary_error = error
            else:
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception) as error:
                    primary_error = error
        except (asyncio.CancelledError, Exception) as error:
            primary_error = error
        finally:
            application_session._deactivate()
            for task in (application_task, reader_task):
                if not task.done():
                    task.cancel()
            application_result, reader_result = await asyncio.gather(
                application_task,
                reader_task,
                return_exceptions=True,
            )
            if session.application_session is application_session:
                session.application_session = None
            if session.application_task is application_task:
                session.application_task = None

        for result in (application_result, reader_result):
            if isinstance(result, BaseException) and not isinstance(
                result,
                asyncio.CancelledError,
            ):
                self._raise_normalized_session_error(result)
        if primary_error is not None:
            if isinstance(primary_error, asyncio.CancelledError) and not session.ready:
                return
            self._raise_normalized_session_error(primary_error)

    async def _async_run_heartbeat(
        self,
        session: BridgeSession,
        application_session: BridgeApplicationSession | None = None,
    ) -> None:
        """Own all session reads, heartbeats, and application dispatch."""
        pending_ping_id: str | None = None
        pending_ping_deadline: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            timeout = (
                max(0.0, pending_ping_deadline - loop.time())
                if pending_ping_deadline is not None
                else HEARTBEAT_INTERVAL
            )
            try:
                async with asyncio.timeout(timeout):
                    payload = await self._async_receive_payload(session)
            except TimeoutError:
                if pending_ping_id is not None:
                    raise _PeerClosed from None
                ping_id = generate_nonce()
                await self._async_send_payload(
                    session,
                    {"id": ping_id, "type": "ping"},
                )
                pending_ping_id = ping_id
                pending_ping_deadline = loop.time() + HEARTBEAT_RESPONSE_TIMEOUT
                continue

            if not isinstance(payload, dict):
                raise ProtocolError("invalid protocol message")
            message_type = payload.get("type")
            if message_type == "ping":
                ping_id = _validate_heartbeat_payload(payload)
                await self._async_send_payload(
                    session,
                    {"id": ping_id, "type": "pong"},
                )
                continue

            if message_type == "control_request":
                if session.selection is None or not session.selection.supports(FEATURE_DOMOTICZ_CONTROL_V1):
                    raise ProtocolError("invalid protocol message")
                try:
                    request = parse_control(session.selection, payload)
                    result = await self._async_handle_control_request(session, request)
                except Exception:
                    raise ProtocolError("invalid protocol message")
                await self._async_send_payload(session, result)
                continue

            if message_type == "pong":
                pong_id = _validate_heartbeat_payload(payload)
                if pending_ping_id is None or pong_id != pending_ping_id:
                    raise ProtocolError("invalid protocol message")
                pending_ping_id = None
                pending_ping_deadline = None
                continue

            if application_session is None:
                raise ProtocolError("invalid protocol message")
            application_session._deliver(payload)

    async def _async_receive_payload(
        self,
        session: BridgeSession,
    ) -> dict[str, object]:
        """Receive one authenticated, in-order client envelope."""
        verified = verify_envelope(
            session.session_key,
            await _async_receive_document(session.websocket),
            protocol_version=(
                session.selection.version
                if session.selection is not None
                else PROTOCOL_VERSION
            ),
            expected_direction=DIRECTION_DOMOTICZ_TO_HA,
            expected_session_id=session.session_id,
            last_sequence=session.client_sequence,
        )
        session.client_sequence = verified.sequence
        return verified.payload

    async def _async_send_payload(
        self,
        session: BridgeSession,
        payload: dict[str, object],
    ) -> None:
        """Send one authenticated, in-order server envelope."""
        async with session.send_lock:
            async with self._lock:
                if self._sessions.get(session.link_id) is not session:
                    raise ProtocolError("application session is no longer active")
            sequence = session.server_sequence + 1
            document = sign_envelope(
                session.session_key,
                protocol_version=(
                    session.selection.version
                    if session.selection is not None
                    else PROTOCOL_VERSION
                ),
                direction=DIRECTION_HA_TO_DOMOTICZ,
                session_id=session.session_id,
                sequence=sequence,
                payload=payload,
            )
            await _async_send_document(session.websocket, document)
            session.server_sequence = sequence

    @staticmethod
    async def _async_close_session(
        session: BridgeSession,
        code: WSCloseCode,
        message: bytes,
    ) -> None:
        """Close a detached session after stopping its application work."""
        await _async_close(session.websocket, code, message)

    async def _async_release_session(self, session: BridgeSession) -> None:
        """Release a session only if it is still the active instance."""
        async with self._lock:
            self._deactivate_session(session)
            if self._sessions.get(session.link_id) is session:
                self._sessions.pop(session.link_id)

    async def _async_mark_ready(self, session: BridgeSession) -> None:
        """Atomically mark only the exact still-active session ready."""
        async with self._lock:
            if self._sessions.get(session.link_id) is not session:
                raise _PeerClosed
            session.ready = True

    @staticmethod
    def _deactivate_session(session: BridgeSession) -> None:
        """Synchronously stop application work for one detached session."""
        session.ready = False
        application_session = session.application_session
        if application_session is not None:
            application_session._deactivate()
        application_task = session.application_task
        if application_task is not None and not application_task.done():
            application_task.cancel()

    @staticmethod
    def _raise_normalized_session_error(error: BaseException) -> None:
        """Raise expected transport failures or one fixed fail-closed error."""
        if not isinstance(error, Exception):
            raise error
        if isinstance(
            error,
            (
                _PeerClosed,
                ProtocolError,
                TimeoutError,
                ConnectionError,
            ),
        ):
            raise error
        raise ProtocolError("application session is unavailable") from None

    async def _async_find_mapped_capability(
        self,
        entry_id: str,
        destination_id: str,
        target_id: str,
    ) -> Optional[Capability]:
        """Find the mapped capability for a given target_id across numeric and binary catalogs."""
        if self._application is None:
            return None
        hass = self._application._hass

        # 1. Check numeric catalog
        num_storage = HomeAssistantCatalogStorage(hass, entry_id, destination_id)
        try:
            num_doc = await num_storage.async_load()
            if num_doc is not None:
                num_catalog = catalog_from_document(num_doc)
                for record in num_catalog:
                    if record.target_id == target_id:
                        return record.capability
        except Exception:
            pass

        # 2. Check binary catalog
        bin_storage = HomeAssistantBinaryCatalogStorage(hass, entry_id, destination_id)
        try:
            bin_doc = await bin_storage.async_load()
            if bin_doc is not None:
                bin_catalog = catalog_from_document(bin_doc)
                for record in bin_catalog:
                    if record.target_id == target_id:
                        return record.capability
        except Exception:
            pass

        return None

    async def _async_handle_control_request(
        self,
        session: BridgeSession,
        request: object,
    ) -> dict[str, object]:
        """Validate, map, and execute one incoming Domoticz control request."""
        # Since request is passed as parsed object (ControlRequest)
        if self._application is None:
            return build_control_result(
                session.selection,
                request.request_id,
                ControlResultStatus.REJECTED,
                error="bridge application is unavailable",
            )
        hass = self._application._hass

        # 1. Find the mapped capability in the target catalogs
        capability = await self._async_find_mapped_capability(
            session.entry_id,
            session.destination_id,
            request.target_id,
        )
        if capability is None:
            return build_control_result(
                session.selection,
                request.request_id,
                ControlResultStatus.REJECTED,
                error=f"target_id {request.target_id!r} is not owned by this session",
            )

        # 2. Get the Home Assistant entity_id using the entity registry
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        entry = registry.async_get(capability.source.object_id)
        if entry is None:
            return build_control_result(
                session.selection,
                request.request_id,
                ControlResultStatus.REJECTED,
                error=f"source entity registry entry {capability.source.object_id!r} not found",
            )

        entity_id = entry.entity_id

        # 3. Map command to Home Assistant service
        cmd = request.command.lower()
        if cmd == "on":
            domain = "homeassistant"
            service = "turn_on"
            data = {"entity_id": entity_id}
        elif cmd == "off":
            domain = "homeassistant"
            service = "turn_off"
            data = {"entity_id": entity_id}
        elif cmd in {"set level", "setlevel", "set_level"}:
            domain = "homeassistant"
            service = "turn_on"
            data = {
                "entity_id": entity_id,
                "brightness_pct": int(request.level),
            }
        else:
            return build_control_result(
                session.selection,
                request.request_id,
                ControlResultStatus.REJECTED,
                error=f"unsupported command {request.command!r}",
            )

        # 4. Call the service safely
        try:
            await hass.services.async_call(
                domain,
                service,
                data,
                blocking=True,
            )
        except Exception as err:
            return build_control_result(
                session.selection,
                request.request_id,
                ControlResultStatus.REJECTED,
                error=f"service call failed: {err}",
            )

        return build_control_result(
            session.selection,
            request.request_id,
            ControlResultStatus.CONFIRMED,
        )


class DomoticzBridgeView(HomeAssistantView):
    """Unauthenticated HTTP upgrade endpoint with protocol-level authentication."""

    name = f"api:{DOMAIN}:websocket"
    url = BRIDGE_WEBSOCKET_PATH
    requires_auth = False
    cors_allowed = False

    def __init__(self, manager: DomoticzBridgeManager) -> None:
        """Initialize the singleton view."""
        self._manager = manager

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Upgrade one bounded Domoticz bridge connection."""
        if not await self._manager.async_reserve_handshake():
            return web.Response(status=HTTPStatus.SERVICE_UNAVAILABLE)

        try:
            client_protocols = _request_protocols(request)
            if (
                client_protocols
                and select_websocket_subprotocol(
                    client_protocols,
                    SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
                )
                is None
            ):
                await self._manager.async_release_handshake()
                return web.Response(status=HTTPStatus.BAD_REQUEST)
        except ProtocolError:
            await self._manager.async_release_handshake()
            return web.Response(status=HTTPStatus.BAD_REQUEST)

        websocket = web.WebSocketResponse(
            autoping=True,
            compress=False,
            max_msg_size=MAX_BRIDGE_MESSAGE_BYTES,
            protocols=SUPPORTED_WEBSOCKET_SUBPROTOCOLS,
        )
        try:
            async with asyncio.timeout(PREPARE_TIMEOUT):
                await websocket.prepare(request)
        except BaseException:
            await self._manager.async_release_handshake()
            raise

        await self._manager.async_handle_reserved(
            websocket,
            client_protocols=client_protocols,
            selected_protocol=websocket.ws_protocol,
        )
        return websocket


async def _async_receive_document(
    websocket: web.WebSocketResponse,
) -> object:
    """Receive one canonical text document or classify a closed peer."""
    message = await websocket.receive()
    if message.type is WSMsgType.TEXT:
        return canonical_json_loads(message.data)
    if message.type in {
        WSMsgType.CLOSE,
        WSMsgType.CLOSED,
        WSMsgType.CLOSING,
        WSMsgType.ERROR,
    }:
        raise _PeerClosed
    raise ProtocolError("invalid protocol message")


async def _async_send_document(
    websocket: web.WebSocketResponse,
    document: object,
) -> None:
    """Send one canonical text document."""
    await websocket.send_str(canonical_json_dumps(document))


def _validate_heartbeat_payload(payload: dict[str, object]) -> str:
    """Validate an exact signed application heartbeat payload."""
    if set(payload) != {"id", "type"}:
        raise ProtocolError("invalid protocol message")
    heartbeat_id = payload["id"]
    validate_nonce(heartbeat_id)
    assert isinstance(heartbeat_id, str)
    return heartbeat_id


def _request_protocols(request: web.Request) -> tuple[str, ...]:
    """Normalize the ordered WebSocket protocol offer from HTTP headers."""
    header_values = request.headers.getall("Sec-WebSocket-Protocol", ())
    if not header_values:
        return ()
    tokens = [
        token.strip()
        for header_value in header_values
        for token in header_value.split(",")
    ]
    return validate_protocol_tokens(tokens)


async def _async_close(
    websocket: web.WebSocketResponse,
    code: WSCloseCode,
    message: bytes,
) -> None:
    """Close a WebSocket without leaking protocol or credential details."""
    if not websocket.closed:
        await websocket.close(code=code, message=message)
