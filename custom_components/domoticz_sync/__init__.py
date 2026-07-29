"""The Domoticz Sync integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .bridge import DomoticzBridgeManager, DomoticzBridgeView
from .bridge_credentials import async_ensure_bridge_credentials
from .const import (
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DATA_BRIDGE_MANAGER,
    DOMAIN,
)
from .coordinator import (
    DomoticzDataUpdateCoordinator,
    DomoticzRuntimeData,
    build_api,
)
from .export_label import async_ensure_export_label

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the singleton companion-plugin endpoint."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if DATA_BRIDGE_MANAGER in domain_data:
        return True

    manager = DomoticzBridgeManager()
    domain_data[DATA_BRIDGE_MANAGER] = manager
    hass.http.register_view(DomoticzBridgeView(manager))

    async def _async_shutdown(_event: Event) -> None:
        await manager.async_shutdown()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Domoticz Sync from a config entry."""
    manager = _bridge_manager(hass)
    credentials = async_ensure_bridge_credentials(hass, entry)
    await manager.async_register_link(
        entry_id=entry.entry_id,
        link_id=credentials.link_id,
        pairing_key=credentials.pairing_key,
    )

    try:
        async_ensure_export_label(hass, entry)

        api = build_api(hass, entry)
        coordinator = DomoticzDataUpdateCoordinator(hass, entry, api)
        await coordinator.async_config_entry_first_refresh()

        entry.runtime_data = DomoticzRuntimeData(api=api, coordinator=coordinator)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await manager.async_unregister_entry(entry.entry_id)
        raise

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Add companion-plugin credentials to entries created before version 2."""
    if entry.version > CONFIG_ENTRY_VERSION or (
        entry.version == CONFIG_ENTRY_VERSION
        and entry.minor_version > CONFIG_ENTRY_MINOR_VERSION
    ):
        return False
    async_ensure_bridge_credentials(
        hass,
        entry,
        version=CONFIG_ENTRY_VERSION,
        minor_version=CONFIG_ENTRY_MINOR_VERSION,
    )
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await _bridge_manager(hass).async_unregister_entry(entry.entry_id)
    return unloaded


def _bridge_manager(hass: HomeAssistant) -> DomoticzBridgeManager:
    """Return the initialized domain bridge manager."""
    manager = hass.data.setdefault(DOMAIN, {}).get(DATA_BRIDGE_MANAGER)
    if not isinstance(manager, DomoticzBridgeManager):
        raise RuntimeError("Domoticz bridge manager is not initialized")
    return manager
