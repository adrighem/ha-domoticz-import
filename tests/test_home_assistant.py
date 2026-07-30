"""Runtime smoke tests inside Home Assistant."""

from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME  # noqa: E402
from homeassistant.core import HomeAssistant, State  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402
from homeassistant.helpers.entity import EntityCategory  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.domoticz_sync import CONFIG_SCHEMA  # noqa: E402
from custom_components.domoticz_sync.api import DomoticzApi  # noqa: E402
from custom_components.domoticz_sync.const import (  # noqa: E402
    CONF_EXPORT_LABEL_ID,
    CONF_VERIFY_SSL,
    DOMAIN,
    EXPORT_LABEL_NAME,
)
from custom_components.domoticz_sync.models import DomoticzDevice  # noqa: E402


def test_integration_declares_config_entry_only_schema() -> None:
    """The integration-level bridge does not imply YAML configuration."""
    assert CONFIG_SCHEMA({}) == {}


def _state_for_source(
    hass: HomeAssistant,
    domain: str,
    source_id: str,
) -> State:
    """Return the single entity state for a Domoticz source."""
    matches = [
        state
        for state in hass.states.async_all(domain)
        if state.attributes.get("domoticz_idx") == source_id
    ]
    assert len(matches) == 1
    return matches[0]


def _devices(temperature: float, motion: str) -> list[DomoticzDevice]:
    """Return a representative Domoticz snapshot."""
    return [
        DomoticzDevice.from_api(
            {
                "idx": "10",
                "Name": "Living room climate",
                "Type": "Temp",
                "Temp": temperature,
                "HardwareName": "RFXCOM",
                "HardwareID": 2,
                "ID": "climate-1",
            }
        ),
        DomoticzDevice.from_api(
            {
                "idx": "11",
                "Name": "Hall motion",
                "Type": "Light/Switch",
                "SwitchType": "Motion Sensor",
                "Status": motion,
                "HardwareName": "Zigbee",
                "HardwareID": 3,
                "ID": "motion-1",
            }
        ),
    ]


@pytest.mark.asyncio
async def test_config_entry_lifecycle_in_home_assistant(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Set up, refresh, and unload representative entities in Home Assistant."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Domoticz test",
        unique_id="http://domoticz.test:8080",
        data={
            CONF_URL: "http://domoticz.test:8080",
            CONF_USERNAME: "",
            CONF_PASSWORD: "",
            CONF_VERIFY_SSL: False,
        },
    )
    entry.add_to_hass(hass)

    get_devices = AsyncMock(
        side_effect=[
            _devices(21.5, "On"),
            _devices(22.0, "Off"),
        ]
    )
    with patch.object(DomoticzApi, "async_get_devices", get_devices):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        temperature = _state_for_source(hass, "sensor", "10")
        motion = _state_for_source(hass, "binary_sensor", "11")
        assert temperature.state == "21.5"
        assert temperature.attributes["domoticz_sync_origin"] == "domoticz"
        assert temperature.attributes["domoticz_sync_source_id"] == "10"
        assert motion.state == "on"

        export_label = lr.async_get(hass).async_get_label_by_name(EXPORT_LABEL_NAME)
        assert export_label is not None
        assert entry.data[CONF_EXPORT_LABEL_ID] == export_label.label_id

        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

        assert _state_for_source(hass, "sensor", "10").state == "22.0"
        assert _state_for_source(hass, "binary_sensor", "11").state == "off"

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert not [
        state
        for state in hass.states.async_all()
        if state.attributes.get("domoticz_sync_origin") == "domoticz"
    ]
    assert lr.async_get(hass).async_get_label(export_label.label_id) is not None


@pytest.mark.asyncio
async def test_diagnostic_sensor_uses_native_entity_category(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Diagnostic metrics expose Home Assistant's EntityCategory enum."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Domoticz diagnostic test",
        unique_id="http://domoticz-diagnostic.test:8080",
        data={
            CONF_URL: "http://domoticz-diagnostic.test:8080",
            CONF_USERNAME: "",
            CONF_PASSWORD: "",
            CONF_VERIFY_SSL: False,
        },
    )
    entry.add_to_hass(hass)
    device = DomoticzDevice.from_api(
        {
            "idx": "12",
            "Name": "Outdoor sensor",
            "Type": "Temp",
            "Temp": 18.5,
            "BatteryLevel": 90,
        }
    )

    with patch.object(
        DomoticzApi,
        "async_get_devices",
        AsyncMock(return_value=[device]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_12_battery_level",
    )
    assert entity_id is not None
    registry_entry = registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_category is EntityCategory.DIAGNOSTIC
