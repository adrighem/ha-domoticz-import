"""Read labelled Home Assistant entities into the neutral capability model."""

from __future__ import annotations

from math import isfinite
from typing import Any

from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    LIGHT_LUX,
    PERCENTAGE,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfVolume,
    UnitOfVolumetricFlux,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .const import EXPORT_LABEL_NAME
from .core import (
    Availability,
    Capability,
    CapabilityKind,
    SourceIdentity,
)
from .export_label import async_get_export_label_id
from .provenance import is_domoticz_mirror

_SOURCE_SYSTEM = "home_assistant"
_STATE_CAPABILITY_ID = "state"

_BINARY_SENSOR_DOMAIN = "binary_sensor"
_SENSOR_DOMAIN = "sensor"

_NON_NUMERIC_SENSOR_DEVICE_CLASSES = {"date", "enum", "timestamp", "uptime"}

_UNIT_MAP = {
    UnitOfTemperature.CELSIUS: "celsius",
    UnitOfTemperature.FAHRENHEIT: "fahrenheit",
    PERCENTAGE: "percent",
    UnitOfPressure.HPA: "hpa",
    UnitOfPressure.BAR: "bar",
    LIGHT_LUX: "lux",
    UnitOfElectricPotential.VOLT: "volt",
    UnitOfElectricCurrent.AMPERE: "A",
    UnitOfFrequency.HERTZ: "hz",
    UnitOfPower.WATT: "watt",
    UnitOfEnergy.KILO_WATT_HOUR: "kwh",
    UnitOfVolume.CUBIC_METERS: "m3",
    UnitOfVolume.LITERS: "l",
    UnitOfPrecipitationDepth.MILLIMETERS: "mm",
    UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR: "mm_per_hour",
    UnitOfSpeed.METERS_PER_SECOND: "meter_per_second",
}


class ExportLabelNotFoundError(LookupError):
    """Raised when the configured Home Assistant export label was deleted."""


async def async_collect_export_capabilities(
    hass: HomeAssistant,
    *,
    label_id: str | None = None,
) -> list[Capability]:
    """Collect capabilities using Home Assistant's stable instance ID."""
    return collect_export_capabilities(
        hass,
        instance_id=await async_get_instance_id(hass),
        label_id=label_id,
    )


@callback
def collect_export_capabilities(
    hass: HomeAssistant,
    *,
    instance_id: str,
    label_id: str | None = None,
) -> list[Capability]:
    """Collect labelled numeric and binary entities without side effects."""
    if label_id is None:
        label_id = async_get_export_label_id(hass)
    if label_id is None:
        raise ExportLabelNotFoundError(
            f"Home Assistant export label {EXPORT_LABEL_NAME!r} does not exist"
        )
    if lr.async_get(hass).async_get_label(label_id) is None:
        raise ExportLabelNotFoundError(
            f"Home Assistant export label {label_id!r} does not exist"
        )

    entries = sorted(
        er.async_entries_for_label(er.async_get(hass), label_id),
        key=lambda entry: entry.id,
    )

    capabilities: list[Capability] = []
    for entry in entries:
        if entry.disabled or entry.domain not in {
            _BINARY_SENSOR_DOMAIN,
            _SENSOR_DOMAIN,
        }:
            continue

        state = hass.states.get(entry.entity_id)
        attributes = state.attributes if state is not None else {}
        if is_domoticz_mirror(platform=entry.platform, attributes=attributes):
            continue

        capability = _capability_from_entry(
            hass,
            entry,
            state,
            instance_id=instance_id,
        )
        if capability is not None:
            capabilities.append(capability)

    return capabilities


def _capability_from_entry(
    hass: HomeAssistant,
    entry: er.RegistryEntry,
    state: State | None,
    *,
    instance_id: str,
) -> Capability | None:
    """Convert one registry entry and current state."""
    attributes = state.attributes if state is not None else {}
    device_class = _first_string(
        attributes.get(ATTR_DEVICE_CLASS),
        entry.device_class,
        entry.original_device_class,
    )
    raw_unit = _first_string(
        attributes.get(ATTR_UNIT_OF_MEASUREMENT),
        entry.unit_of_measurement,
    )
    state_class = _first_string(
        attributes.get(ATTR_STATE_CLASS),
        (entry.capabilities or {}).get(ATTR_STATE_CLASS),
    )

    source = SourceIdentity(
        system=_SOURCE_SYSTEM,
        instance_id=instance_id,
        object_id=entry.id,
        capability_id=_STATE_CAPABILITY_ID,
    )
    name = (
        state.name
        if state is not None
        else er.async_get_full_entity_name(hass, entry) or entry.entity_id
    )

    if entry.domain == _BINARY_SENSOR_DOMAIN:
        return _binary_capability(source, name, device_class, state)

    unit = _normalized_unit(raw_unit)
    return _numeric_capability(
        source,
        name,
        device_class,
        unit,
        state_class,
        state,
    )


def _binary_capability(
    source: SourceIdentity,
    name: str,
    semantic: str | None,
    state: State | None,
) -> Capability | None:
    """Convert a binary sensor state."""
    if state is None or state.state == STATE_UNAVAILABLE:
        availability = Availability.UNAVAILABLE
        value = None
    elif state.state == STATE_UNKNOWN:
        availability = Availability.UNKNOWN
        value = None
    elif state.state == STATE_ON:
        availability = Availability.AVAILABLE
        value = True
    elif state.state == STATE_OFF:
        availability = Availability.AVAILABLE
        value = False
    else:
        return None

    return Capability(
        source=source,
        kind=CapabilityKind.BINARY,
        name=name,
        value=value,
        availability=availability,
        semantic=semantic,
    )


def _numeric_capability(
    source: SourceIdentity,
    name: str,
    semantic: str | None,
    unit: str | None,
    state_class: str | None,
    state: State | None,
) -> Capability | None:
    """Convert a numeric sensor state."""
    if semantic in _NON_NUMERIC_SENSOR_DEVICE_CLASSES:
        return None

    if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        if not _has_numeric_metadata(semantic, unit, state_class):
            return None
        availability = (
            Availability.UNKNOWN
            if state is not None and state.state == STATE_UNKNOWN
            else Availability.UNAVAILABLE
        )
        return Capability(
            source=source,
            kind=CapabilityKind.NUMERIC,
            name=name,
            value=None,
            availability=availability,
            semantic=semantic,
            unit=unit,
            state_class=state_class,
        )

    try:
        value = float(state.state)
    except ValueError:
        return None
    if not isfinite(value):
        return None

    return Capability(
        source=source,
        kind=CapabilityKind.NUMERIC,
        name=name,
        value=value,
        semantic=semantic,
        unit=unit,
        state_class=state_class,
    )


def _has_numeric_metadata(
    semantic: str | None,
    unit: str | None,
    state_class: str | None,
) -> bool:
    """Return whether an unavailable sensor is known to be numeric."""
    return any(
        (
            semantic is not None,
            unit is not None,
            state_class is not None,
        )
    )


def _first_string(*values: Any) -> str | None:
    """Return the first non-empty string representation."""
    for value in values:
        if value is None:
            continue
        string_value = str(value).strip()
        if string_value:
            return string_value
    return None


def _normalized_unit(unit: str | None) -> str | None:
    """Translate common Home Assistant units to neutral unit keys."""
    if unit is None:
        return None
    return _UNIT_MAP.get(unit, unit)
