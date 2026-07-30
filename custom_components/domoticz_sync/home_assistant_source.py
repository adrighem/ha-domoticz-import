"""Read labelled Home Assistant entities into the neutral capability model."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
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


class ExportExclusionReason(StrEnum):
    """Fixed, log-safe reason why one directly labelled entity was excluded."""

    DISABLED = "entity is disabled"
    UNSUPPORTED_DOMAIN = "entity domain is not supported"
    DOMOTICZ_MIRROR = "Domoticz-origin entity cannot be exported back"
    NON_NUMERIC_DEVICE_CLASS = "sensor device class is not numeric"
    INVALID_NUMERIC_STATE = "sensor state is not a finite number"
    MISSING_NUMERIC_METADATA = "unknown or unavailable sensor lacks numeric metadata"
    INVALID_BINARY_STATE = "binary sensor state is invalid"
    CAPABILITY_KIND_NOT_ENABLED = "entity type is not enabled for export"


@dataclass(frozen=True, slots=True)
class ExportExclusion:
    """One actionable exclusion without source state or attribute data."""

    entity_id: str
    reason: ExportExclusionReason


@dataclass(frozen=True, slots=True)
class ExportCollection:
    """Exportable capabilities and safe diagnostics for one label snapshot."""

    capabilities: tuple[Capability, ...]
    exclusions: tuple[ExportExclusion, ...]


async def async_collect_export_capabilities(
    hass: HomeAssistant,
    *,
    label_id: str | None = None,
) -> list[Capability]:
    """Collect capabilities using Home Assistant's stable instance ID."""
    collection = collect_export_selection(
        hass,
        instance_id=await async_get_instance_id(hass),
        label_id=label_id,
    )
    return list(collection.capabilities)


@callback
def collect_export_capabilities(
    hass: HomeAssistant,
    *,
    instance_id: str,
    label_id: str | None = None,
) -> list[Capability]:
    """Collect labelled numeric and binary entities without side effects."""
    return list(
        collect_export_selection(
            hass,
            instance_id=instance_id,
            label_id=label_id,
        ).capabilities
    )


@callback
def collect_export_selection(
    hass: HomeAssistant,
    *,
    instance_id: str,
    label_id: str | None = None,
    included_kinds: Collection[CapabilityKind] | None = None,
) -> ExportCollection:
    """Collect labelled entities and explain every direct exclusion."""
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

    if included_kinds is None:
        included_kinds = frozenset(CapabilityKind)

    capabilities: list[Capability] = []
    exclusions: list[ExportExclusion] = []
    for entry in entries:
        if entry.disabled:
            exclusions.append(
                ExportExclusion(entry.entity_id, ExportExclusionReason.DISABLED)
            )
            continue
        if entry.domain not in {
            _BINARY_SENSOR_DOMAIN,
            _SENSOR_DOMAIN,
        }:
            exclusions.append(
                ExportExclusion(
                    entry.entity_id,
                    ExportExclusionReason.UNSUPPORTED_DOMAIN,
                )
            )
            continue

        state = hass.states.get(entry.entity_id)
        attributes = state.attributes if state is not None else {}
        if is_domoticz_mirror(platform=entry.platform, attributes=attributes):
            exclusions.append(
                ExportExclusion(
                    entry.entity_id,
                    ExportExclusionReason.DOMOTICZ_MIRROR,
                )
            )
            continue

        capability, reason = _capability_from_entry(
            hass,
            entry,
            state,
            instance_id=instance_id,
        )
        if reason is not None:
            exclusions.append(ExportExclusion(entry.entity_id, reason))
            continue
        assert capability is not None
        if capability.kind not in included_kinds:
            exclusions.append(
                ExportExclusion(
                    entry.entity_id,
                    ExportExclusionReason.CAPABILITY_KIND_NOT_ENABLED,
                )
            )
            continue
        capabilities.append(capability)

    return ExportCollection(tuple(capabilities), tuple(exclusions))


def _capability_from_entry(
    hass: HomeAssistant,
    entry: er.RegistryEntry,
    state: State | None,
    *,
    instance_id: str,
) -> tuple[Capability | None, ExportExclusionReason | None]:
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
) -> tuple[Capability | None, ExportExclusionReason | None]:
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
        return None, ExportExclusionReason.INVALID_BINARY_STATE

    return (
        Capability(
            source=source,
            kind=CapabilityKind.BINARY,
            name=name,
            value=value,
            availability=availability,
            semantic=semantic,
        ),
        None,
    )


def _numeric_capability(
    source: SourceIdentity,
    name: str,
    semantic: str | None,
    unit: str | None,
    state_class: str | None,
    state: State | None,
) -> tuple[Capability | None, ExportExclusionReason | None]:
    """Convert a numeric sensor state."""
    if semantic in _NON_NUMERIC_SENSOR_DEVICE_CLASSES:
        return None, ExportExclusionReason.NON_NUMERIC_DEVICE_CLASS

    if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        if not _has_numeric_metadata(semantic, unit, state_class):
            return None, ExportExclusionReason.MISSING_NUMERIC_METADATA
        availability = (
            Availability.UNKNOWN
            if state is not None and state.state == STATE_UNKNOWN
            else Availability.UNAVAILABLE
        )
        return (
            Capability(
                source=source,
                kind=CapabilityKind.NUMERIC,
                name=name,
                value=None,
                availability=availability,
                semantic=semantic,
                unit=unit,
                state_class=state_class,
            ),
            None,
        )

    try:
        value = float(state.state)
    except ValueError:
        return None, ExportExclusionReason.INVALID_NUMERIC_STATE
    if not isfinite(value):
        return None, ExportExclusionReason.INVALID_NUMERIC_STATE

    return (
        Capability(
            source=source,
            kind=CapabilityKind.NUMERIC,
            name=name,
            value=value,
            semantic=semantic,
            unit=unit,
            state_class=state_class,
        ),
        None,
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
