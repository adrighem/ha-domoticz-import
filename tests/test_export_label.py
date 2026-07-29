"""Tests for provisioning the Home Assistant export label."""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.domoticz_sync.const import (  # noqa: E402
    CONF_EXPORT_LABEL_ID,
    DATA_EXPORT_LABEL_ID,
    DOMAIN,
    EXPORT_LABEL_ID,
    EXPORT_LABEL_NAME,
)
from custom_components.domoticz_sync.export_label import (  # noqa: E402
    async_ensure_export_label,
)
from custom_components.domoticz_sync.home_assistant_source import (  # noqa: E402
    collect_export_capabilities,
)


def _config_entry(
    hass: HomeAssistant,
    *,
    label_id: str | None = None,
) -> ConfigEntry:
    """Create a minimal registered config entry."""
    data = {CONF_EXPORT_LABEL_ID: label_id} if label_id is not None else {}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    return entry


def test_creates_and_remembers_missing_export_label(hass: HomeAssistant) -> None:
    """Successful setup can provision the label without manual preparation."""
    entry = _config_entry(hass)

    label_id = async_ensure_export_label(hass, entry)

    label = lr.async_get(hass).async_get_label(label_id)
    assert label is not None
    assert label.label_id == EXPORT_LABEL_ID
    assert label.name == EXPORT_LABEL_NAME
    assert entry.data[CONF_EXPORT_LABEL_ID] == label_id
    assert hass.data[DOMAIN][DATA_EXPORT_LABEL_ID] == label_id


def test_discovers_existing_export_label_by_name(hass: HomeAssistant) -> None:
    """An existing user-created label is reused instead of duplicated."""
    existing = lr.async_get(hass).async_create(EXPORT_LABEL_NAME)
    entry = _config_entry(hass)

    label_id = async_ensure_export_label(hass, entry)

    assert label_id == existing.label_id
    assert len(list(lr.async_get(hass).async_list_labels())) == 1
    assert entry.data[CONF_EXPORT_LABEL_ID] == existing.label_id


def test_persisted_label_id_survives_user_rename(hass: HomeAssistant) -> None:
    """The remembered ID remains authoritative when the display name changes."""
    registry = lr.async_get(hass)
    existing = registry.async_create(EXPORT_LABEL_NAME)
    registry.async_update(existing.label_id, name="Send to Domoticz")
    entry = _config_entry(hass, label_id=existing.label_id)

    label_id = async_ensure_export_label(hass, entry)

    assert label_id == existing.label_id
    assert registry.async_get_label(label_id).name == "Send to Domoticz"
    assert len(list(registry.async_list_labels())) == 1


def test_peer_entry_recovers_renamed_suffixed_label_after_restart(
    hass: HomeAssistant,
) -> None:
    """All Domoticz entries converge on one global label across restarts."""
    registry = lr.async_get(hass)
    occupied = registry.async_create(EXPORT_LABEL_NAME)
    registry.async_update(occupied.label_id, name="Unrelated label")
    first_entry = _config_entry(hass)
    export_label_id = async_ensure_export_label(hass, first_entry)
    registry.async_update(export_label_id, name="Send to Domoticz")

    hass.data.pop(DOMAIN)
    second_entry = _config_entry(hass)
    recovered_label_id = async_ensure_export_label(hass, second_entry)

    assert recovered_label_id == export_label_id
    assert second_entry.data[CONF_EXPORT_LABEL_ID] == export_label_id
    assert len(list(registry.async_list_labels())) == 2


def test_stale_persisted_label_id_is_replaced(hass: HomeAssistant) -> None:
    """A deleted remembered label is safely rediscovered or recreated."""
    entry = _config_entry(hass, label_id="deleted-label")

    label_id = async_ensure_export_label(hass, entry)

    assert label_id == EXPORT_LABEL_ID
    assert entry.data[CONF_EXPORT_LABEL_ID] == EXPORT_LABEL_ID
    assert lr.async_get(hass).async_get_label(label_id).name == EXPORT_LABEL_NAME


def test_label_id_collision_is_persisted_and_used_for_collection(
    hass: HomeAssistant,
) -> None:
    """A renamed label occupying the canonical ID cannot break selection."""
    label_registry = lr.async_get(hass)
    occupied = label_registry.async_create(EXPORT_LABEL_NAME)
    label_registry.async_update(occupied.label_id, name="Unrelated label")
    entry = _config_entry(hass)

    label_id = async_ensure_export_label(hass, entry)

    assert label_id != EXPORT_LABEL_ID
    assert label_registry.async_get_label(label_id).name == EXPORT_LABEL_NAME
    assert entry.data[CONF_EXPORT_LABEL_ID] == label_id

    entity_registry = er.async_get(hass)
    source = entity_registry.async_get_or_create(
        "sensor",
        "test",
        "selected",
        suggested_object_id="selected",
    )
    entity_registry.async_update_entity(source.entity_id, labels={label_id})
    hass.states.async_set(source.entity_id, "42")

    capabilities = collect_export_capabilities(hass, instance_id="ha-instance")

    assert [capability.source.object_id for capability in capabilities] == [source.id]


def test_collection_before_setup_does_not_assume_canonical_label_id(
    hass: HomeAssistant,
) -> None:
    """A colliding unrelated label cannot be selected before setup."""
    registry = lr.async_get(hass)
    occupied = registry.async_create(EXPORT_LABEL_NAME)
    registry.async_update(occupied.label_id, name="Unrelated label")

    with pytest.raises(LookupError, match="does not exist"):
        collect_export_capabilities(hass, instance_id="ha-instance")
