"""Diagnostics tests for secret redaction."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import CONF_PASSWORD, CONF_URL  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.domoticz_sync.const import (  # noqa: E402
    CONF_LINK_ID,
    CONF_PAIRING_KEY,
    DOMAIN,
)
from custom_components.domoticz_sync.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)


@pytest.mark.asyncio
async def test_diagnostics_redact_all_credentials(hass: HomeAssistant) -> None:
    """Neither config-entry data nor defensive options can expose secrets."""
    password = "test-domoticz-password"
    pairing_key = "A" * 43
    defensive_options_key = "B" * 43
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Domoticz test",
        data={
            CONF_URL: "http://domoticz.test:8080",
            CONF_PASSWORD: password,
            CONF_LINK_ID: "link_test_pairing",
            CONF_PAIRING_KEY: pairing_key,
        },
        options={CONF_PAIRING_KEY: defensive_options_key},
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"][CONF_PASSWORD] != password
    assert diagnostics["entry"][CONF_PAIRING_KEY] != pairing_key
    assert diagnostics["options"][CONF_PAIRING_KEY] != defensive_options_key
    assert diagnostics["entry"][CONF_LINK_ID] == "link_test_pairing"
