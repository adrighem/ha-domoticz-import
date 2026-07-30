"""Tests for the Domoticz API client."""

import asyncio
from base64 import b64encode
from hmac import compare_digest
from unittest.mock import MagicMock

import pytest
from aiohttp import ClientResponseError

import custom_components.domoticz_sync.api as api_module
from custom_components.domoticz_sync.api import (
    DomoticzApi,
    DomoticzApiError,
    DomoticzAuthError,
    DomoticzConnectionError,
    normalize_base_url,
)


def test_legacy_basic_auth_encoder_fallback(monkeypatch):
    """Use aiohttp's legacy encoder when the modern helper is unavailable."""
    state = {"constructed": False, "encoded": False}

    class LegacyBasicAuth:
        """Test double for aiohttp.BasicAuth."""

        def __init__(self, username, password, encoding):
            if (
                username != "placeholder-user"
                or password != "placeholder-password"
                or encoding != "latin1"
            ):
                raise AssertionError("Legacy Basic Auth arguments did not match")
            state["constructed"] = True

        def encode(self):
            state["encoded"] = True
            return "fallback-result"

    monkeypatch.setattr(api_module, "_aiohttp_encode_basic_auth", None)
    monkeypatch.setattr(api_module, "_legacy_basic_auth", LegacyBasicAuth)

    result = api_module._encode_basic_auth_header(
        "placeholder-user",
        "placeholder-password",
    )

    assert result == "fallback-result"
    assert state == {"constructed": True, "encoded": True}


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
        normalize_base_url("https://domoticz.example.com:8443/domoticz")
        == "https://domoticz.example.com:8443/domoticz"
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
    api = DomoticzApi(
        session,
        "http://domoticz.local:8080",
        "test-user",
        "test-password",
    )

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
    assert "auth" not in kwargs
    headers = kwargs["headers"]
    assert set(headers) == {"Authorization"}
    expected_header = "Basic " + b64encode(
        b"test-user:test-password",
    ).decode("ascii")
    if not compare_digest(
        headers["Authorization"],
        expected_header,
    ):
        raise AssertionError("Authorization header encoding mismatch")


def test_get_server_time_auth_error():
    """Test HTTP auth failures are classified."""
    session = MagicMock()
    session.get.return_value = MockResponse(status=401)
    api = DomoticzApi(session, "http://domoticz.local:8080")

    try:
        asyncio.run(api.async_get_server_time())
    except DomoticzAuthError:
        kwargs = session.get.call_args.kwargs
        assert "auth" not in kwargs
        assert kwargs["headers"] is None
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
