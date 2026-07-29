"""Tests for the Domoticz API client."""

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientResponseError

from custom_components.domoticz_sync.api import (
    DomoticzApi,
    DomoticzApiError,
    DomoticzAuthError,
    DomoticzConnectionError,
    normalize_base_url,
)


class MockResponse:
    """Async context manager response for aiohttp-style calls."""

    def __init__(self, status=200, json_data=None, raise_error=None):
        """Initialize the response."""
        self.status = status
        self._json_data = json_data if json_data is not None else {}
        self._raise_error = raise_error

    async def __aenter__(self):
        """Enter the response context."""
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """Exit the response context."""
        return None

    async def json(self, content_type=None):
        """Return JSON payload."""
        return self._json_data

    def raise_for_status(self):
        """Raise a configured HTTP error."""
        if self._raise_error is not None:
            raise self._raise_error


def test_normalize_base_url():
    """Test Domoticz URL normalization."""
    assert normalize_base_url("192.168.1.20:8080") == "http://192.168.1.20:8080"
    assert (
        normalize_base_url("https://domoticz.local:8443/some/path?x=1")
        == "https://domoticz.local:8443/some/path"
    )
    assert (
        normalize_base_url("https://domoticz.local:8443/some/path/json.htm")
        == "https://domoticz.local:8443/some/path"
    )
    assert (
        normalize_base_url("https://domoticz.local:8443/json.htm")
        == "https://domoticz.local:8443"
    )
    assert (
        normalize_base_url("https://xmpp.vanadrighem.eu:8443/domoticz")
        == "https://xmpp.vanadrighem.eu:8443/domoticz"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://embedded-user@domoticz.local",
        "http://embedded-user:embedded-value@domoticz.local",
    ],
)
def test_normalize_base_url_rejects_embedded_credentials(url):
    """Credentials must use the dedicated fields and never enter URLs."""
    with pytest.raises(DomoticzApiError, match="must not contain"):
        normalize_base_url(url)


def test_get_devices_sends_expected_params():
    """Test getdevices request and response parsing."""
    session = MagicMock()
    session.get.return_value = MockResponse(
        json_data={
            "status": "OK",
            "result": [
                {"idx": "1", "Name": "Kitchen", "Type": "Temp", "Temp": 19.5},
                {"Name": "Ignored without idx"},
            ],
        }
    )
    api = DomoticzApi(session, "http://domoticz.local:8080", "user", "secret")

    devices = asyncio.run(
        api.async_get_devices(include_hidden=True, favorite_only=True)
    )

    assert len(devices) == 1
    assert devices[0].idx == "1"
    url = session.get.call_args.args[0]
    kwargs = session.get.call_args.kwargs
    assert url == "http://domoticz.local:8080/json.htm"
    assert kwargs["params"]["param"] == "getdevices"
    assert kwargs["params"]["displayhidden"] == "1"
    assert kwargs["params"]["favorite"] == "1"
    assert kwargs["auth"].login == "user"


def test_get_server_time_auth_error():
    """Test HTTP auth failures are classified."""
    session = MagicMock()
    session.get.return_value = MockResponse(status=401)
    api = DomoticzApi(session, "http://domoticz.local:8080")

    try:
        asyncio.run(api.async_get_server_time())
    except DomoticzAuthError:
        return
    raise AssertionError("Expected DomoticzAuthError")


def test_http_errors_are_connection_errors():
    """Test non-auth HTTP failures are classified as connection errors."""
    session = MagicMock()
    error = ClientResponseError(None, (), status=500)
    session.get.return_value = MockResponse(status=500, raise_error=error)
    api = DomoticzApi(session, "http://domoticz.local:8080")

    try:
        asyncio.run(api.async_get_server_time())
    except DomoticzConnectionError:
        return
    raise AssertionError("Expected DomoticzConnectionError")


def test_domoticz_application_error():
    """Test Domoticz ERROR payloads are classified."""
    session = MagicMock()
    session.get.return_value = MockResponse(
        json_data={"status": "ERROR", "message": "Bad request"}
    )
    api = DomoticzApi(session, "http://domoticz.local:8080")

    try:
        asyncio.run(api.async_get_server_time())
    except DomoticzApiError:
        return
    raise AssertionError("Expected DomoticzApiError")
