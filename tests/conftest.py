"""Test isolation helpers."""

import sys
from importlib.util import find_spec
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


if find_spec("homeassistant") is None:
    custom_components = ModuleType("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]
    sys.modules.setdefault("custom_components", custom_components)

    domoticz_sync = ModuleType("custom_components.domoticz_sync")
    domoticz_sync.__path__ = [str(ROOT / "custom_components" / "domoticz_sync")]
    sys.modules.setdefault("custom_components.domoticz_sync", domoticz_sync)


if find_spec("aiohttp") is None:
    aiohttp = ModuleType("aiohttp")

    class BasicAuth:
        """Small aiohttp BasicAuth stand-in for tests."""

        def __init__(self, login, password=""):
            """Initialize auth."""
            self.login = login
            self.password = password

    class ClientError(Exception):
        """Small aiohttp ClientError stand-in."""

    class ClientResponseError(ClientError):
        """Small aiohttp ClientResponseError stand-in."""

        def __init__(self, request_info, history, *, status=0):
            """Initialize response error."""
            super().__init__(f"HTTP {status}")
            self.request_info = request_info
            self.history = history
            self.status = status

    class ClientSession:
        """Small aiohttp ClientSession stand-in."""

    aiohttp.BasicAuth = BasicAuth
    aiohttp.ClientError = ClientError
    aiohttp.ClientResponseError = ClientResponseError
    aiohttp.ClientSession = ClientSession
    sys.modules.setdefault("aiohttp", aiohttp)
