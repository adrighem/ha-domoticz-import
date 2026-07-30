"""Async Domoticz JSON API client."""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from aiohttp import ClientError, ClientResponseError, ClientSession

try:
    from aiohttp import encode_basic_auth as _aiohttp_encode_basic_auth
except ImportError:
    from aiohttp import BasicAuth as _legacy_basic_auth

    _aiohttp_encode_basic_auth = None
else:
    _legacy_basic_auth = None

from .models import DomoticzDevice


class DomoticzError(Exception):
    """Base exception for Domoticz API errors."""


class DomoticzConnectionError(DomoticzError):
    """Raised when Domoticz cannot be reached."""


class DomoticzAuthError(DomoticzError):
    """Raised when Domoticz rejects credentials."""


class DomoticzApiError(DomoticzError):
    """Raised when Domoticz returns an application-level error."""


class DomoticzApi:
    """Small client for the Domoticz JSON API."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self._session = session
        self.base_url = normalize_base_url(base_url)
        self._headers = (
            {
                "Authorization": _encode_basic_auth_header(
                    username,
                    password or "",
                )
            }
            if username or password
            else None
        )

    async def async_get_server_time(self) -> dict[str, Any]:
        """Fetch Domoticz server time as a lightweight connectivity check."""
        return await self._request({"type": "command", "param": "getServerTime"})

    async def async_get_devices(
        self,
        *,
        include_hidden: bool = False,
        favorite_only: bool = False,
    ) -> list[DomoticzDevice]:
        """Fetch all used Domoticz devices."""
        params: dict[str, str] = {
            "type": "command",
            "param": "getdevices",
            "filter": "all",
            "used": "true",
            "order": "Name",
        }
        if include_hidden:
            params["displayhidden"] = "1"
        if favorite_only:
            params["favorite"] = "1"

        data = await self._request(params)
        result = data.get("result") or []
        if not isinstance(result, list):
            raise DomoticzApiError("Domoticz returned an invalid devices response")

        return [
            DomoticzDevice.from_api(item)
            for item in result
            if isinstance(item, dict) and (item.get("idx") or item.get("Idx"))
        ]

    async def _request(self, params: dict[str, str]) -> dict[str, Any]:
        """Perform a GET request against json.htm."""
        url = urljoin(f"{self.base_url}/", "json.htm")
        try:
            async with self._session.get(
                url,
                params=params,
                headers=self._headers,
            ) as resp:
                if resp.status in (401, 403):
                    raise DomoticzAuthError("Invalid Domoticz credentials")

                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except DomoticzAuthError:
            raise
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise DomoticzAuthError("Invalid Domoticz credentials") from err
            raise DomoticzConnectionError(
                f"Domoticz returned HTTP {err.status}"
            ) from err
        except ClientError as err:
            raise DomoticzConnectionError(
                f"Failed to connect to Domoticz: {err}"
            ) from err
        except TimeoutError as err:
            raise DomoticzConnectionError("Timed out connecting to Domoticz") from err
        except ValueError as err:
            raise DomoticzApiError("Domoticz returned invalid JSON") from err

        if not isinstance(data, dict):
            raise DomoticzApiError("Domoticz returned an invalid response")
        if data.get("status") == "ERROR":
            raise DomoticzApiError(str(data.get("message") or "Domoticz API error"))

        return data


def _encode_basic_auth_header(username: str | None, password: str) -> str:
    """Encode credentials across supported aiohttp versions."""
    login = username or ""
    if _aiohttp_encode_basic_auth is not None:
        return _aiohttp_encode_basic_auth(
            login,
            password,
            encoding="latin1",
        )
    if _legacy_basic_auth is None:
        raise RuntimeError("aiohttp does not provide a Basic Auth encoder")
    return _legacy_basic_auth(login, password, encoding="latin1").encode()


def normalize_base_url(base_url: str) -> str:
    """Return a normalized Domoticz base URL without trailing slash."""
    raw_url = base_url.strip()
    parsed = urlparse(raw_url)
    if not parsed.scheme or (
        parsed.scheme not in {"http", "https"} and not parsed.netloc
    ):
        parsed = urlparse(f"http://{raw_url}")

    if parsed.scheme not in {"http", "https"}:
        raise DomoticzApiError("Domoticz URL must use http or https")
    if not parsed.netloc:
        raise DomoticzApiError("Domoticz URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise DomoticzApiError("Domoticz URL must not contain embedded credentials")

    path = parsed.path.rstrip("/")
    if path.endswith("/json.htm"):
        path = path[:-9]
    elif path == "/json.htm":
        path = ""

    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")
