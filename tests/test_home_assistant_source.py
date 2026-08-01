"""Tests for collecting labelled Home Assistant source entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components.binary_sensor import (  # noqa: E402
    BinarySensorDeviceClass,
)
from homeassistant.components.sensor import (  # noqa: E402
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (  # noqa: E402
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UV_INDEX,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant  # noqa: E402
from homeassistant.helpers import device_registry as dr  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.domoticz_sync.const import (  # noqa: E402
    DATA_EXPORT_LABEL_ID,
    DOMAIN,
    EXPORT_LABEL_ID,
)
from custom_components.domoticz_sync.core import (  # noqa: E402
    Availability,
    CapabilityKind,
    CompoundCapability,
)
from custom_components.domoticz_sync.home_assistant_source import (  # noqa: E402
    ExportExclusionReason,
    ExportLabelNotFoundError,
    async_collect_export_capabilities,
    async_subscribe_export_changes,
    collect_export_capabilities,
    collect_export_selection,
)


@pytest.fixture(autouse=True)
def create_export_label(hass: HomeAssistant) -> None:
    """Create the configured export label for each Home Assistant test."""
    label = lr.async_get(hass).async_create("Domoticz Export")
    assert label.label_id == EXPORT_LABEL_ID
    hass.data.setdefault(DOMAIN, {})[DATA_EXPORT_LABEL_ID] = label.label_id


def _register_entity(
    hass: HomeAssistant,
    domain: str,
    unique_id: str,
    *,
    platform: str = "test",
    labelled: bool = True,
    disabled: bool = False,
    device_class: str | None = None,
    state_class: str | None = None,
    unit: str | None = None,
    device_id: str | None = None,
) -> er.RegistryEntry:
    """Register one source entity for collection tests."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain,
        platform,
        unique_id,
        suggested_object_id=unique_id,
        capabilities={ATTR_STATE_CLASS: state_class} if state_class else None,
        disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
        original_device_class=device_class,
        original_name=unique_id.replace("_", " ").title(),
        unit_of_measurement=unit,
        device_id=device_id,
    )
    if labelled:
        entry = registry.async_update_entity(
            entry.entity_id,
            labels={EXPORT_LABEL_ID},
        )
    return entry


def test_collects_labelled_controllable_switches(hass: HomeAssistant) -> None:
    """Explicitly labelled switch domains become binary capabilities."""
    switch = _register_entity(hass, "switch", "test_switch")
    helper = _register_entity(hass, "input_boolean", "test_helper")
    hass.states.async_set(switch.entity_id, STATE_ON)
    hass.states.async_set(helper.entity_id, STATE_OFF)

    capabilities = collect_export_capabilities(hass, instance_id="ha-instance")

    by_source = {capability.source.object_id: capability for capability in capabilities}
    assert by_source[switch.id].kind is CapabilityKind.BINARY
    assert by_source[switch.id].value is True
    assert by_source[helper.id].kind is CapabilityKind.BINARY
    assert by_source[helper.id].value is False


def test_groups_one_temperature_humidity_pair_per_device(
    hass: HomeAssistant,
) -> None:
    """A physical device pair becomes one native compound capability."""
    config_entry = MockConfigEntry(domain="test", entry_id="test-entry")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "climate-sensor")},
        name="Climate Sensor",
    )
    temperature = _register_entity(
        hass,
        "sensor",
        "climate_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        device_id=device.id,
    )
    humidity = _register_entity(
        hass,
        "sensor",
        "climate_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        unit="%",
        device_id=device.id,
    )
    hass.states.async_set(temperature.entity_id, "21.5")
    hass.states.async_set(humidity.entity_id, "48")

    capabilities = collect_export_capabilities(hass, instance_id="ha-instance")

    assert len(capabilities) == 1
    compound = capabilities[0]
    assert isinstance(compound, CompoundCapability)
    assert compound.name == "Climate Sensor"
    assert compound.source.object_id == device.id
    assert compound.source.capability_id == "temperature_humidity"
    assert [part.semantic for part in compound.capabilities] == [
        "temperature",
        "humidity",
    ]


def test_collects_labelled_numeric_and_binary_states(hass: HomeAssistant) -> None:
    """Labelled numeric and binary entities become neutral capabilities."""
    temperature = _register_entity(
        hass,
        "sensor",
        "living_room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
    )
    motion = _register_entity(
        hass,
        "binary_sensor",
        "hall_motion",
        device_class=BinarySensorDeviceClass.MOTION,
    )
    hass.states.async_set(
        temperature.entity_id,
        "21.5",
        {
            ATTR_DEVICE_CLASS: SensorDeviceClass.TEMPERATURE,
            ATTR_FRIENDLY_NAME: "Living room temperature",
            ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.CELSIUS,
        },
    )
    hass.states.async_set(
        motion.entity_id,
        STATE_ON,
        {
            ATTR_DEVICE_CLASS: BinarySensorDeviceClass.MOTION,
            ATTR_FRIENDLY_NAME: "Hall motion",
        },
    )

    capabilities = collect_export_capabilities(hass, instance_id="ha-instance")

    assert len(capabilities) == 2
    by_source = {capability.source.object_id: capability for capability in capabilities}
    numeric = by_source[temperature.id]
    binary = by_source[motion.id]
    assert numeric.source.instance_id == "ha-instance"
    assert numeric.source.object_id == temperature.id
    assert numeric.source.capability_id == "state"
    assert numeric.kind is CapabilityKind.NUMERIC
    assert numeric.name == "Living room temperature"
    assert numeric.value == 21.5
    assert numeric.semantic == "temperature"
    assert numeric.unit == "celsius"
    assert binary.source.object_id == motion.id
    assert binary.kind is CapabilityKind.BINARY
    assert binary.value is True
    assert binary.semantic == "motion"


def test_selection_excludes_unlabelled_disabled_and_mirrored_entities(
    hass: HomeAssistant,
) -> None:
    """Selection is explicit and Domoticz mirrors cannot loop back."""
    selected = _register_entity(hass, "sensor", "selected")
    unlabelled = _register_entity(
        hass,
        "sensor",
        "unlabelled",
        labelled=False,
    )
    disabled = _register_entity(
        hass,
        "sensor",
        "disabled",
        disabled=True,
    )
    own_mirror = _register_entity(
        hass,
        "sensor",
        "own_mirror",
        platform="domoticz_sync",
    )
    explicit_mirror = _register_entity(hass, "sensor", "explicit_mirror")
    legacy_mirror = _register_entity(hass, "sensor", "legacy_mirror")

    for entry in (selected, unlabelled, disabled, own_mirror):
        hass.states.async_set(entry.entity_id, "1")
    hass.states.async_set(
        explicit_mirror.entity_id,
        "1",
        {"domoticz_sync_origin": "domoticz"},
    )
    hass.states.async_set(legacy_mirror.entity_id, "1", {"domoticz_idx": "42"})

    capabilities = collect_export_capabilities(hass, instance_id="ha-instance")

    assert [capability.source.object_id for capability in capabilities] == [selected.id]


def test_selection_explains_every_directly_labelled_exclusion(
    hass: HomeAssistant,
) -> None:
    """Diagnostics contain only actionable entity IDs and fixed reasons."""
    selected = _register_entity(hass, "sensor", "selected")
    disabled = _register_entity(hass, "sensor", "disabled", disabled=True)
    unsupported = _register_entity(hass, "input_text", "unsupported")
    mirror = _register_entity(
        hass,
        "sensor",
        "mirror",
        platform="domoticz_sync",
    )
    enum = _register_entity(
        hass,
        "sensor",
        "enum",
        device_class=SensorDeviceClass.ENUM,
    )
    invalid_numeric = _register_entity(hass, "sensor", "invalid_numeric")
    unknown_generic = _register_entity(hass, "sensor", "unknown_generic")
    invalid_binary = _register_entity(hass, "binary_sensor", "invalid_binary")
    valid_binary = _register_entity(hass, "binary_sensor", "valid_binary")

    hass.states.async_set(selected.entity_id, "1")
    hass.states.async_set(disabled.entity_id, "private-disabled-value")
    hass.states.async_set(unsupported.entity_id, "private-unsupported-value")
    hass.states.async_set(mirror.entity_id, "1")
    hass.states.async_set(enum.entity_id, "private-enum-value")
    hass.states.async_set(invalid_numeric.entity_id, "private-invalid-value")
    hass.states.async_set(unknown_generic.entity_id, STATE_UNKNOWN)
    hass.states.async_set(invalid_binary.entity_id, "private-binary-value")
    hass.states.async_set(valid_binary.entity_id, STATE_ON)

    collection = collect_export_selection(
        hass,
        instance_id="ha-instance",
        included_kinds=frozenset({CapabilityKind.NUMERIC}),
    )

    assert [item.source.object_id for item in collection.capabilities] == [selected.id]
    assert {
        exclusion.entity_id: exclusion.reason for exclusion in collection.exclusions
    } == {
        disabled.entity_id: ExportExclusionReason.DISABLED,
        unsupported.entity_id: ExportExclusionReason.UNSUPPORTED_DOMAIN,
        mirror.entity_id: ExportExclusionReason.DOMOTICZ_MIRROR,
        enum.entity_id: ExportExclusionReason.NON_NUMERIC_DEVICE_CLASS,
        invalid_numeric.entity_id: ExportExclusionReason.INVALID_NUMERIC_STATE,
        unknown_generic.entity_id: ExportExclusionReason.MISSING_NUMERIC_METADATA,
        invalid_binary.entity_id: ExportExclusionReason.INVALID_BINARY_STATE,
        valid_binary.entity_id: ExportExclusionReason.CAPABILITY_KIND_NOT_ENABLED,
    }
    diagnostic_text = repr(collection.exclusions)
    assert "private-disabled-value" not in diagnostic_text
    assert "private-unsupported-value" not in diagnostic_text
    assert "private-enum-value" not in diagnostic_text
    assert "private-invalid-value" not in diagnostic_text
    assert "private-binary-value" not in diagnostic_text


def test_distinguishes_missing_label_from_valid_empty_selection(
    hass: HomeAssistant,
) -> None:
    """A deleted label cannot be mistaken for intentional removal."""
    assert not collect_export_capabilities(hass, instance_id="ha-instance")

    with pytest.raises(ExportLabelNotFoundError, match="does not exist"):
        collect_export_capabilities(
            hass,
            instance_id="ha-instance",
            label_id="deleted_label",
        )


def test_label_rename_keeps_selection_by_stable_id(hass: HomeAssistant) -> None:
    """Selection stores Home Assistant's label ID rather than its mutable name."""
    entry = _register_entity(hass, "sensor", "selected")
    hass.states.async_set(entry.entity_id, "1")
    lr.async_get(hass).async_update(EXPORT_LABEL_ID, name="Send to Domoticz")

    capabilities = collect_export_capabilities(hass, instance_id="ha-instance")

    assert capabilities[0].source.object_id == entry.id


def test_export_change_subscription_requires_the_explicit_label(
    hass: HomeAssistant,
) -> None:
    """A missing configured label cannot be treated as an empty selection."""
    with pytest.raises(ExportLabelNotFoundError, match="does not exist"):
        async_subscribe_export_changes(
            hass,
            label_id="deleted-label",
            on_change=Mock(),
        )


@pytest.mark.asyncio
async def test_export_change_subscription_tracks_selected_supported_states(
    hass: HomeAssistant,
) -> None:
    """State and attribute changes include directly labelled exclusions only."""
    selected = _register_entity(hass, "sensor", "selected")
    disabled = _register_entity(hass, "binary_sensor", "disabled", disabled=True)
    unlabelled = _register_entity(
        hass,
        "sensor",
        "unlabelled",
        labelled=False,
    )
    unsupported = _register_entity(hass, "input_text", "unsupported")
    controllable = _register_entity(hass, "input_boolean", "controllable")
    on_change = Mock()
    unsubscribe = async_subscribe_export_changes(
        hass,
        label_id=EXPORT_LABEL_ID,
        on_change=on_change,
    )

    hass.states.async_set(selected.entity_id, "1")
    hass.states.async_set(disabled.entity_id, STATE_ON)
    hass.states.async_set(unlabelled.entity_id, "2")
    hass.states.async_set(unsupported.entity_id, "private-value")
    hass.states.async_set(controllable.entity_id, STATE_ON)
    await hass.async_block_till_done()

    assert on_change.call_count == 3
    assert all(not call.args and not call.kwargs for call in on_change.call_args_list)

    on_change.reset_mock()
    hass.states.async_set(
        selected.entity_id,
        "1",
        {ATTR_FRIENDLY_NAME: "Updated display name"},
    )
    await hass.async_block_till_done()

    on_change.assert_called_once_with()
    unsubscribe()


@pytest.mark.asyncio
async def test_export_change_subscription_replaces_state_ids_during_relabel(
    hass: HomeAssistant,
) -> None:
    """Relabeling adds and removes state tracking without an event-loop gap."""
    entry = _register_entity(hass, "sensor", "relabelled", labelled=False)
    hass.states.async_set(entry.entity_id, "1")
    registry = er.async_get(hass)
    on_change = Mock()
    unsubscribe = async_subscribe_export_changes(
        hass,
        label_id=EXPORT_LABEL_ID,
        on_change=on_change,
    )

    registry.async_update_entity(entry.entity_id, labels={EXPORT_LABEL_ID})
    hass.states.async_set(entry.entity_id, "2")
    await hass.async_block_till_done()

    assert on_change.call_count == 2
    assert all(not call.args and not call.kwargs for call in on_change.call_args_list)

    on_change.reset_mock()
    registry.async_update_entity(entry.entity_id, labels=set())
    hass.states.async_set(entry.entity_id, "3")
    await hass.async_block_till_done()

    on_change.assert_called_once_with()
    unsubscribe()


@pytest.mark.asyncio
async def test_export_change_subscription_tracks_registry_metadata_and_rename(
    hass: HomeAssistant,
) -> None:
    """Selected registry metadata and renamed state IDs remain observable."""
    selected = _register_entity(hass, "sensor", "registry_selected")
    unlabelled = _register_entity(
        hass,
        "sensor",
        "registry_unlabelled",
        labelled=False,
    )
    registry = er.async_get(hass)
    on_change = Mock()
    unsubscribe = async_subscribe_export_changes(
        hass,
        label_id=EXPORT_LABEL_ID,
        on_change=on_change,
    )

    registry.async_update_entity(unlabelled.entity_id, name="Ignored metadata")
    await hass.async_block_till_done()
    on_change.assert_not_called()

    registry.async_update_entity(
        selected.entity_id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    registry.async_update_entity(selected.entity_id, name="Updated metadata")
    await hass.async_block_till_done()
    assert on_change.call_count == 2

    on_change.reset_mock()
    renamed = registry.async_update_entity(
        selected.entity_id,
        new_entity_id="sensor.registry_renamed",
    )
    hass.states.async_set(renamed.entity_id, "4")
    await hass.async_block_till_done()

    assert on_change.call_count == 2
    assert all(not call.args and not call.kwargs for call in on_change.call_args_list)
    unsubscribe()


@pytest.mark.asyncio
async def test_export_change_subscription_handles_label_lifecycle_and_cleanup(
    hass: HomeAssistant,
) -> None:
    """The stable label can be renamed, while deletion and cleanup are safe."""
    selected = _register_entity(hass, "sensor", "label_lifecycle")
    hass.states.async_set(selected.entity_id, "1")
    label_registry = lr.async_get(hass)
    on_change = Mock()
    unsubscribe = async_subscribe_export_changes(
        hass,
        label_id=EXPORT_LABEL_ID,
        on_change=on_change,
    )

    label_registry.async_update(EXPORT_LABEL_ID, name="Send to Domoticz")
    await hass.async_block_till_done()
    on_change.assert_not_called()

    label_registry.async_delete(EXPORT_LABEL_ID)
    await hass.async_block_till_done()
    assert on_change.call_count >= 1
    assert all(not call.args and not call.kwargs for call in on_change.call_args_list)

    on_change.reset_mock()
    hass.states.async_set(selected.entity_id, "2")
    await hass.async_block_till_done()
    on_change.assert_not_called()

    unsubscribe()
    unsubscribe()


@pytest.mark.asyncio
async def test_export_change_unsubscribe_makes_queued_callbacks_inert(
    hass: HomeAssistant,
) -> None:
    """A queued state callback cannot outlive its idempotent unsubscribe."""
    selected = _register_entity(hass, "sensor", "queued_callback")
    on_change = Mock()
    state_callbacks = []
    remove_state_listener = Mock()

    def track_state_change(_hass, entity_ids, action):
        assert entity_ids == [selected.entity_id]
        state_callbacks.append(action)
        return remove_state_listener

    with patch(
        "custom_components.domoticz_sync.home_assistant_source."
        "async_track_state_change_event",
        side_effect=track_state_change,
    ):
        unsubscribe = async_subscribe_export_changes(
            hass,
            label_id=EXPORT_LABEL_ID,
            on_change=on_change,
        )

    unsubscribe()
    unsubscribe()
    state_callbacks[0](
        Event(
            "state_changed",
            {
                "entity_id": selected.entity_id,
                "new_state": None,
                "old_state": None,
            },
        )
    )

    on_change.assert_not_called()
    remove_state_listener.assert_called_once_with()


@pytest.mark.parametrize(
    ("state_value", "expected"),
    (
        ("21", 21.0),
        ("21.25", 21.25),
        ("1e3", 1000.0),
    ),
)
def test_parses_supported_numeric_shapes(
    hass: HomeAssistant,
    state_value: str,
    expected: float,
) -> None:
    """Integer, decimal, and exponent sensor states are numeric."""
    entry = _register_entity(
        hass,
        "sensor",
        f"numeric_{state_value}",
    )
    hass.states.async_set(entry.entity_id, state_value)

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.value == expected


@pytest.mark.parametrize(
    ("state_value", "expected"),
    (
        (STATE_UNKNOWN, Availability.UNKNOWN),
        (STATE_UNAVAILABLE, Availability.UNAVAILABLE),
    ),
)
def test_preserves_numeric_availability(
    hass: HomeAssistant,
    state_value: str,
    expected: Availability,
) -> None:
    """Unknown and unavailable numeric sensors do not become zero."""
    entry = _register_entity(
        hass,
        "sensor",
        f"temperature_{state_value}",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
    )
    hass.states.async_set(
        entry.entity_id,
        state_value,
        {
            ATTR_DEVICE_CLASS: SensorDeviceClass.TEMPERATURE,
            ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.CELSIUS,
            "state_class": SensorStateClass.MEASUREMENT,
        },
    )

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.kind is CapabilityKind.NUMERIC
    assert capability.value is None
    assert capability.availability is expected


@pytest.mark.parametrize(
    ("state_value", "expected_value", "expected_availability"),
    (
        (STATE_ON, True, Availability.AVAILABLE),
        (STATE_OFF, False, Availability.AVAILABLE),
        (STATE_UNKNOWN, None, Availability.UNKNOWN),
        (STATE_UNAVAILABLE, None, Availability.UNAVAILABLE),
    ),
)
def test_maps_all_binary_states(
    hass: HomeAssistant,
    state_value: str,
    expected_value: bool | None,
    expected_availability: Availability,
) -> None:
    """Binary values and availability remain distinct."""
    entry = _register_entity(
        hass,
        "binary_sensor",
        f"motion_{state_value}",
        device_class=BinarySensorDeviceClass.MOTION,
    )
    hass.states.async_set(entry.entity_id, state_value)

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.value is expected_value
    assert capability.availability is expected_availability


def test_skips_text_non_finite_and_non_numeric_sensor_states(
    hass: HomeAssistant,
) -> None:
    """Step 3 does not widen its scope to text or invalid numeric values."""
    text = _register_entity(hass, "sensor", "text")
    non_finite = _register_entity(hass, "sensor", "non_finite")
    numeric_enum = _register_entity(
        hass,
        "sensor",
        "numeric_enum",
        device_class=SensorDeviceClass.ENUM,
    )
    numeric_uptime = _register_entity(
        hass,
        "sensor",
        "numeric_uptime",
        device_class=SensorDeviceClass.UPTIME,
    )
    invalid_binary = _register_entity(hass, "binary_sensor", "invalid_binary")
    unknown_generic = _register_entity(hass, "sensor", "unknown_generic")
    hass.states.async_set(text.entity_id, "Tomorrow: paper")
    hass.states.async_set(non_finite.entity_id, "nan")
    hass.states.async_set(
        numeric_enum.entity_id,
        "1",
        {ATTR_DEVICE_CLASS: SensorDeviceClass.ENUM},
    )
    hass.states.async_set(
        numeric_uptime.entity_id,
        "1",
        {ATTR_DEVICE_CLASS: SensorDeviceClass.UPTIME},
    )
    hass.states.async_set(invalid_binary.entity_id, "maybe")
    hass.states.async_set(unknown_generic.entity_id, STATE_UNKNOWN)

    assert not collect_export_capabilities(hass, instance_id="ha-instance")


def test_every_sensor_device_class_has_an_explicit_export_decision() -> None:
    """A new Home Assistant sensor class must receive an intentional policy."""
    native_when_metadata_matches = {
        "atmospheric_pressure",
        "battery",
        "carbon_dioxide",
        "current",
        "distance",
        "humidity",
        "illuminance",
        "irradiance",
        "moisture",
        "power",
        "power_factor",
        "pressure",
        "sound_pressure",
        "temperature",
        "voltage",
        "weight",
    }
    custom_sensor = {
        "absolute_humidity",
        "apparent_power",
        "aqi",
        "area",
        "blood_glucose_concentration",
        "carbon_monoxide",
        "conductivity",
        "data_rate",
        "data_size",
        "duration",
        "energy",
        "energy_distance",
        "energy_storage",
        "frequency",
        "gas",
        "monetary",
        "nitrogen_dioxide",
        "nitrogen_monoxide",
        "nitrous_oxide",
        "ozone",
        "ph",
        "pm1",
        "pm10",
        "pm25",
        "pm4",
        "precipitation",
        "precipitation_intensity",
        "reactive_energy",
        "reactive_power",
        "signal_strength",
        "speed",
        "sulphur_dioxide",
        "temperature_delta",
        "volatile_organic_compounds",
        "volatile_organic_compounds_parts",
        "volume",
        "volume_flow_rate",
        "volume_storage",
        "water",
        "wind_direction",
        "wind_speed",
    }
    excluded_non_numeric = {"date", "enum", "timestamp", "uptime"}

    decisions = native_when_metadata_matches | custom_sensor | excluded_non_numeric

    assert native_when_metadata_matches.isdisjoint(custom_sensor)
    assert decisions == {device_class.value for device_class in SensorDeviceClass}


def test_every_binary_sensor_device_class_has_an_explicit_export_decision() -> None:
    """A new Home Assistant binary class must receive an intentional policy."""
    native_switch_profile = {
        "door",
        "garage_door",
        "lock",
        "motion",
        "opening",
        "smoke",
        "window",
    }
    generic_switch_profile = {
        "battery",
        "battery_charging",
        "carbon_monoxide",
        "cold",
        "connectivity",
        "gas",
        "heat",
        "light",
        "moisture",
        "moving",
        "occupancy",
        "plug",
        "power",
        "presence",
        "problem",
        "running",
        "safety",
        "sound",
        "tamper",
        "update",
        "vibration",
    }

    assert native_switch_profile.isdisjoint(generic_switch_profile)
    assert native_switch_profile | generic_switch_profile == {
        device_class.value for device_class in BinarySensorDeviceClass
    }


def test_enabled_binary_kind_is_selected_without_an_exclusion(
    hass: HomeAssistant,
) -> None:
    """Negotiated binary export does not retain its previous disabled warning."""
    entry = _register_entity(
        hass,
        "binary_sensor",
        "selected_motion",
        device_class=BinarySensorDeviceClass.MOTION,
    )
    hass.states.async_set(entry.entity_id, STATE_ON)

    collection = collect_export_selection(
        hass,
        instance_id="ha-instance",
        included_kinds=frozenset({CapabilityKind.BINARY}),
    )

    assert [capability.source.object_id for capability in collection.capabilities] == [
        entry.id
    ]
    assert collection.exclusions == ()


def test_registry_id_survives_entity_id_rename(hass: HomeAssistant) -> None:
    """Mutable entity IDs do not participate in exported identity."""
    entry = _register_entity(hass, "sensor", "original")
    hass.states.async_set(entry.entity_id, "1")
    original = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    er.async_get(hass).async_update_entity(
        entry.entity_id,
        new_entity_id="sensor.renamed",
    )
    hass.states.async_remove(entry.entity_id)
    hass.states.async_set("sensor.renamed", "2")
    renamed = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert renamed.source == original.source
    assert renamed.value == 2.0


def test_missing_typed_state_becomes_unavailable(hass: HomeAssistant) -> None:
    """A known numeric kind remains present when its state is missing."""
    entry = _register_entity(
        hass,
        "sensor",
        "missing_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
    )

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.source.object_id == entry.id
    assert capability.value is None
    assert capability.availability is Availability.UNAVAILABLE


def test_current_state_metadata_overrides_registry_metadata(
    hass: HomeAssistant,
) -> None:
    """Conversion reflects Home Assistant's effective displayed metadata."""
    entry = _register_entity(
        hass,
        "sensor",
        "converted_value",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unit="W",
    )
    hass.states.async_set(
        entry.entity_id,
        "20",
        {
            ATTR_DEVICE_CLASS: SensorDeviceClass.TEMPERATURE,
            ATTR_STATE_CLASS: SensorStateClass.MEASUREMENT,
            ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.CELSIUS,
        },
    )

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.semantic == "temperature"
    assert capability.state_class == "measurement"
    assert capability.unit == "celsius"


def test_current_state_falls_back_to_registry_metadata(
    hass: HomeAssistant,
) -> None:
    """Partially restored state retains the registry's numeric metadata."""
    entry = _register_entity(
        hass,
        "sensor",
        "restored_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        unit=UnitOfTemperature.CELSIUS,
    )
    hass.states.async_set(entry.entity_id, "20")

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.semantic == "temperature"
    assert capability.state_class == "measurement"
    assert capability.unit == "celsius"


def test_numeric_sensor_without_state_class_preserves_absence(
    hass: HomeAssistant,
) -> None:
    """A numeric value does not gain state-class semantics implicitly."""
    entry = _register_entity(hass, "sensor", "unclassified_numeric")
    hass.states.async_set(entry.entity_id, "20")

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.state_class is None


def test_uv_index_unit_is_preserved_for_native_domoticz_selection(
    hass: HomeAssistant,
) -> None:
    """The official unit carries UV semantics when HA has no device class."""
    entry = _register_entity(
        hass,
        "sensor",
        "uv_index",
        state_class=SensorStateClass.MEASUREMENT,
        unit=UV_INDEX,
    )
    hass.states.async_set(entry.entity_id, "6.25")

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.semantic is None
    assert capability.unit == UV_INDEX
    assert capability.value == 6.25


def test_missing_state_uses_registry_state_class(
    hass: HomeAssistant,
) -> None:
    """Registry state class proves a missing generic sensor is numeric."""
    entry = _register_entity(
        hass,
        "sensor",
        "missing_measurement",
        state_class=SensorStateClass.MEASUREMENT,
    )

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.source.object_id == entry.id
    assert capability.value is None
    assert capability.availability is Availability.UNAVAILABLE
    assert capability.semantic is None
    assert capability.unit is None
    assert capability.state_class == "measurement"


@pytest.mark.asyncio
async def test_async_collection_uses_stable_home_assistant_instance_id(
    hass: HomeAssistant,
) -> None:
    """The async entry point supplies Home Assistant's stable instance ID."""
    entry = _register_entity(hass, "sensor", "power")
    hass.states.async_set(entry.entity_id, "532")

    with patch(
        "custom_components.domoticz_sync.home_assistant_source.async_get_instance_id",
        new=AsyncMock(return_value="stable-instance-id"),
    ):
        capabilities = await async_collect_export_capabilities(hass)

    assert capabilities[0].source.instance_id == "stable-instance-id"
