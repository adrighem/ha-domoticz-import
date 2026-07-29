"""Provision the Home Assistant label used to select exported entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import label_registry as lr

from .const import (
    CONF_EXPORT_LABEL_ID,
    DATA_EXPORT_LABEL_ID,
    DOMAIN,
    EXPORT_LABEL_NAME,
)


def _label_for_id(
    registry: lr.LabelRegistry,
    label_id: object,
) -> lr.LabelEntry | None:
    """Return a label only for a usable persisted identifier."""
    if not isinstance(label_id, str) or not label_id:
        return None
    return registry.async_get_label(label_id)


def _persisted_labels(
    hass: HomeAssistant,
    registry: lr.LabelRegistry,
    *,
    current_entry_id: str | None = None,
) -> list[lr.LabelEntry]:
    """Return valid labels remembered by other integration entries."""
    entries = sorted(
        hass.config_entries.async_entries(DOMAIN),
        key=lambda candidate: candidate.entry_id,
    )
    return [
        label
        for candidate in entries
        if candidate.entry_id != current_entry_id
        and (
            label := _label_for_id(
                registry,
                candidate.data.get(CONF_EXPORT_LABEL_ID),
            )
        )
        is not None
    ]


@callback
def async_ensure_export_label(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> str:
    """Create or rediscover the export label and remember its stable ID."""
    registry = lr.async_get(hass)
    configured_label_id = entry.data.get(CONF_EXPORT_LABEL_ID)
    domain_data = hass.data.get(DOMAIN)
    runtime_label_id = (
        domain_data.get(DATA_EXPORT_LABEL_ID) if isinstance(domain_data, dict) else None
    )
    label = _label_for_id(registry, runtime_label_id)

    if label is None:
        label = _label_for_id(registry, configured_label_id)
    if label is None:
        persisted_labels = _persisted_labels(
            hass,
            registry,
            current_entry_id=entry.entry_id,
        )
        label = persisted_labels[0] if persisted_labels else None
    if label is None:
        label = registry.async_get_label_by_name(EXPORT_LABEL_NAME)
    if label is None:
        label = registry.async_create(EXPORT_LABEL_NAME)

    label_id = label.label_id
    if configured_label_id != label_id:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_EXPORT_LABEL_ID: label_id},
        )

    hass.data.setdefault(DOMAIN, {})[DATA_EXPORT_LABEL_ID] = label_id
    return label_id


@callback
def async_get_export_label_id(hass: HomeAssistant) -> str | None:
    """Return a safely resolved label ID without assuming a canonical ID."""
    registry = lr.async_get(hass)
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        label_id = domain_data.get(DATA_EXPORT_LABEL_ID)
        if label := _label_for_id(registry, label_id):
            return label.label_id

    persisted_labels = _persisted_labels(hass, registry)
    if persisted_labels:
        return persisted_labels[0].label_id
    if label := registry.async_get_label_by_name(EXPORT_LABEL_NAME):
        return label.label_id
    return None
