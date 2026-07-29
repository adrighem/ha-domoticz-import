"""End-to-end coverage for the Domoticz plugin and Home Assistant bridge."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from aiohttp import WSMsgType  # noqa: E402
from aiohttp import client as aiohttp_client  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.typing import (  # noqa: E402
    ClientSessionGenerator,
)

from custom_components.domoticz_sync.bridge import (  # noqa: E402
    DomoticzBridgeManager,
    DomoticzBridgeView,
)
from custom_components.domoticz_sync.core.protocol import (  # noqa: E402
    generate_link_id,
    generate_pairing_key,
)

ROOT = Path(__file__).resolve().parents[1]
_MISSING = object()


class _FakeConnection:
    """Translate Domoticz connection callbacks into an in-memory send queue."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.sent: list[dict[str, object]] = []
        self.connecting = False
        self.connected = False
        self.disconnected = False

    def Connect(self) -> None:
        self.connecting = True

    def Connected(self) -> bool:
        return self.connected

    def Connecting(self) -> bool:
        return self.connecting

    def Send(self, document: dict[str, object]) -> None:
        self.sent.append(document)

    def Disconnect(self) -> None:
        self.connecting = False
        self.connected = False
        self.disconnected = True


class _FakeDomoticz(ModuleType):
    """Minimal DomoticzEx module used to load the real root plugin."""

    def __init__(self) -> None:
        super().__init__("DomoticzEx")
        self.configuration: dict[str, object] = {}
        self.connections: list[_FakeConnection] = []
        self.logs: list[str] = []
        self.errors: list[str] = []

    def Configuration(
        self,
        config: object = _MISSING,
    ) -> dict[str, object]:
        if config is not _MISSING:
            assert isinstance(config, dict)
            self.configuration = dict(config)
        return dict(self.configuration)

    def Connection(self, **kwargs: object) -> _FakeConnection:
        connection = _FakeConnection(**kwargs)
        self.connections.append(connection)
        return connection

    def Heartbeat(self, _seconds: int) -> None:
        pass

    def Log(self, message: str) -> None:
        self.logs.append(message)

    def Error(self, message: str) -> None:
        self.errors.append(message)


def _load_plugin(
    monkeypatch: pytest.MonkeyPatch,
    *,
    address: str,
    port: int,
    link_id: str,
    pairing_key: str,
) -> tuple[ModuleType, _FakeDomoticz]:
    """Load root plugin.py with the test's callback-native Domoticz module."""
    fake_domoticz = _FakeDomoticz()
    monkeypatch.setitem(sys.modules, "DomoticzEx", fake_domoticz)

    module_name = "domoticz_plugin_bridge_integration"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    specification = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "plugin.py",
    )
    assert specification is not None
    assert specification.loader is not None
    plugin_module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, module_name, plugin_module)
    specification.loader.exec_module(plugin_module)
    plugin_module.Parameters = {
        "Address": address,
        "Port": str(port),
        "Mode1": "WS",
        "Mode2": link_id,
        "Mode3": pairing_key,
        "HomeFolder": str(ROOT),
    }
    return plugin_module, fake_domoticz


def _next_text_payload(
    connection: _FakeConnection,
    position: int,
) -> tuple[str, int]:
    """Take the next Domoticz WebSocket text send from the fake connection."""
    document = connection.sent[position]
    payload = document.get("Payload")
    assert isinstance(payload, str)
    return payload, position + 1


async def _receive_text(websocket: object) -> str:
    """Receive one real aiohttp WebSocket text frame with a bounded wait."""
    async with asyncio.timeout(2):
        message = await websocket.receive()
    assert message.type is WSMsgType.TEXT
    assert isinstance(message.data, str)
    return message.data


@pytest.mark.asyncio
async def test_root_plugin_and_real_bridge_reach_ready_and_exchange_ping(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped plugin interoperates with the registered HA endpoint."""
    manager = DomoticzBridgeManager()
    link_id = generate_link_id()
    pairing_key = generate_pairing_key()
    await manager.async_register_link(
        entry_id="integration-entry",
        link_id=link_id,
        pairing_key=pairing_key,
    )

    assert await async_setup_component(hass, "http", {})
    assert hass.http is not None
    hass.http.register_view(DomoticzBridgeView(manager))
    client = await hass_client_no_auth()
    endpoint = client.make_url("/")
    assert endpoint.port is not None

    plugin_module, fake_domoticz = _load_plugin(
        monkeypatch,
        address=endpoint.host,
        port=endpoint.port,
        link_id=link_id,
        pairing_key=pairing_key,
    )
    plugin = plugin_module.DomoticzSyncPlugin()
    plugin.onStart()
    connection = fake_domoticz.connections[-1]
    connection.connecting = False
    connection.connected = True
    plugin.onConnect(connection, 0, "connected")

    upgrade_request = connection.sent[0]
    assert upgrade_request["URL"] == plugin_module._ENDPOINT
    request_headers = upgrade_request["Headers"]
    assert isinstance(request_headers, dict)
    upgrade_key = request_headers["Sec-WebSocket-Key"]
    assert isinstance(upgrade_key, str)
    decoded_upgrade_key = base64.b64decode(upgrade_key, validate=True)

    # aiohttp normally generates its own key. Supplying the plugin's key here
    # lets the real server response flow back through the real plugin validator.
    with monkeypatch.context() as websocket_key_patch:
        websocket_key_patch.setattr(
            aiohttp_client,
            "os",
            SimpleNamespace(urandom=lambda size: decoded_upgrade_key),
        )
        websocket = await client.ws_connect(
            upgrade_request["URL"],
            origin=request_headers["Origin"],
            compress=0,
        )

    try:
        response = websocket._response
        plugin.onMessage(
            connection,
            {
                "Status": str(response.status),
                "Headers": dict(response.headers),
            },
        )

        send_position = 1
        hello, send_position = _next_text_payload(connection, send_position)
        await websocket.send_str(hello)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        authenticate, send_position = _next_text_payload(
            connection,
            send_position,
        )
        await websocket.send_str(authenticate)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        inventory, send_position = _next_text_payload(connection, send_position)
        await websocket.send_str(inventory)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        for _ in range(20):
            if await manager.async_is_ready(link_id):
                break
            await asyncio.sleep(0)
        assert plugin.phase == plugin_module.PHASE_READY
        assert await manager.async_is_ready(link_id)

        for _ in range(plugin_module._PING_INTERVAL_TICKS):
            plugin.onHeartbeat()
        ping, send_position = _next_text_payload(connection, send_position)
        await websocket.send_str(ping)
        plugin.onMessage(
            connection,
            {"Payload": await _receive_text(websocket)},
        )

        assert plugin.phase == plugin_module.PHASE_READY
        assert plugin._pending_ping_id is None
        assert send_position == len(connection.sent)
        assert fake_domoticz.errors == []
    finally:
        await websocket.close()
        plugin.onStop()

    for _ in range(20):
        if await manager.async_active_session_count() == 0:
            break
        await asyncio.sleep(0)
    assert await manager.async_active_session_count() == 0
