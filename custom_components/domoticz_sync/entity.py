"""Base entities for Domoticz Sync."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DomoticzDataUpdateCoordinator
from .models import DomoticzDevice
from .provenance import domoticz_provenance_attributes


class DomoticzEntity(CoordinatorEntity[DomoticzDataUpdateCoordinator]):
    """Base class for Domoticz entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DomoticzDataUpdateCoordinator,
        entry: ConfigEntry,
        device: DomoticzDevice,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._idx = device.idx
        self._device_name = device.name

    @property
    def domoticz_device(self) -> DomoticzDevice | None:
        """Return the latest Domoticz device for this entity."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.devices.get(self._idx)

    @property
    def available(self) -> bool:
        """Return if the entity is available."""
        return super().available and self.domoticz_device is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information."""
        device = self.domoticz_device
        hardware_name = device.hardware_name if device else None

        # Determine the best unique grouping identifier for this physical device.
        # If the device has a specific hardware ID and physical address (device_id),
        # use those to group multi-channel sensors/switches under one physical device.
        # Fallback to the logical 'idx' database record.
        if device and device.hardware_id is not None and device.device_id:
            device_identifier = f"{device.hardware_id}_{device.device_id}"
        else:
            device_identifier = self._idx

        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id, device_identifier)},
            name=self._device_name,
            manufacturer="Domoticz",
            model=device.type if device else None,
            sw_version=hardware_name,
            configuration_url=f"{self.coordinator.base_url}/#/Devices",
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return useful Domoticz metadata."""
        device = self.domoticz_device
        if device is None:
            return domoticz_provenance_attributes(self._idx)

        attributes = domoticz_provenance_attributes(device.idx)
        if device.type:
            attributes["domoticz_type"] = device.type
        if device.sub_type:
            attributes["domoticz_sub_type"] = device.sub_type
        if device.switch_type:
            attributes["domoticz_switch_type"] = device.switch_type
        if device.last_update:
            attributes["domoticz_last_update"] = device.last_update
        return attributes
