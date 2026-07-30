"""Config flow for Domoticz Sync."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DomoticzApi,
    DomoticzApiError,
    DomoticzAuthError,
    DomoticzConnectionError,
    normalize_base_url,
)
from .bridge_credentials import (
    generate_bridge_credentials,
    read_bridge_credentials,
)
from .const import (
    CONF_FAVORITE_ONLY,
    CONF_INCLUDE_HIDDEN,
    CONF_LINK_ID,
    CONF_PAIRING_KEY,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_URL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .core.protocol import ProtocolError

_LOGGER = logging.getLogger(__name__)
_UNAVAILABLE_PAIRING_CREDENTIAL = "Unavailable"
_EXPECTED_CONNECTION_ERRORS = (DomoticzApiError, DomoticzConnectionError)


def _user_schema(user_input: dict[str, Any] | None = None) -> vol.Schema:
    """Return the user step schema."""
    user_input = user_input or {}
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=user_input.get(CONF_URL, DEFAULT_URL)): str,
            vol.Optional(CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")): str,
            vol.Optional(CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")): str,
            vol.Optional(
                CONF_VERIFY_SSL,
                default=user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ): bool,
        }
    )


def _options_schema(entry: config_entries.ConfigEntry) -> vol.Schema:
    """Return the options schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_INCLUDE_HIDDEN,
                default=entry.options.get(CONF_INCLUDE_HIDDEN, False),
            ): bool,
            vol.Optional(
                CONF_FAVORITE_ONLY,
                default=entry.options.get(CONF_FAVORITE_ONLY, False),
            ): bool,
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=3600)),
        }
    )


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    base_url = normalize_base_url(data[CONF_URL])
    session = async_get_clientsession(
        hass, verify_ssl=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
    )
    api = DomoticzApi(
        session,
        base_url,
        data.get(CONF_USERNAME),
        data.get(CONF_PASSWORD),
    )
    await api.async_get_server_time()
    return {"title": f"Domoticz ({base_url})", "base_url": base_url}


class DomoticzSyncConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Domoticz Sync."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_user_schema())

        errors: dict[str, str] = {}
        try:
            info = await validate_input(self.hass, user_input)
        except DomoticzAuthError:
            errors["base"] = "invalid_auth"
        except _EXPECTED_CONNECTION_ERRORS:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error validating Domoticz connection")
            errors["base"] = "unknown"
        else:
            await self.async_set_unique_id(info["base_url"])
            self._abort_if_unique_id_configured()
            credentials = generate_bridge_credentials()
            data = {
                CONF_URL: info["base_url"],
                CONF_USERNAME: user_input.get(CONF_USERNAME, ""),
                CONF_PASSWORD: user_input.get(CONF_PASSWORD, ""),
                CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                CONF_LINK_ID: credentials.link_id,
                CONF_PAIRING_KEY: credentials.pairing_key,
            }
            return self.async_create_entry(title=info["title"], data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Domoticz Sync options."""

    def _pairing_placeholders(self) -> dict[str, str]:
        """Return credentials for display without changing the config entry."""
        try:
            credentials = read_bridge_credentials(self.config_entry)
        except ProtocolError:
            _LOGGER.error(
                "Stored companion bridge credentials are invalid "
                "and cannot be displayed"
            )
            return {
                CONF_LINK_ID: _UNAVAILABLE_PAIRING_CREDENTIAL,
                CONF_PAIRING_KEY: _UNAVAILABLE_PAIRING_CREDENTIAL,
            }
        return {
            CONF_LINK_ID: credentials.link_id,
            CONF_PAIRING_KEY: credentials.pairing_key,
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(self.config_entry),
            description_placeholders=self._pairing_placeholders(),
        )
