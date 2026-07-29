"""Tests for collecting labelled Home Assistant source entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.helpers import label_registry as lr  # noqa: E402

from custom_components.domoticz_sync.const import (  # noqa: E402
    DATA_EXPORT_LABEL_ID,
    DOMAIN,
    EXPORT_LABEL_ID,
)
from custom_components.domoticz_sync.core import (  # noqa: E402
    Availability,
    CapabilityKind,
)
from custom_components.domoticz_sync.home_assistant_source import (  # noqa: E402
    ExportLabelNotFoundError,
    async_collect_export_capabilities,
    collect_export_capabilities,
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
    )
    if labelled:
        entry = registry.async_update_entity(
            entry.entity_id,
            labels={EXPORT_LABEL_ID},
        )
    return entry


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
        unit="W",
    )
    hass.states.async_set(
        entry.entity_id,
        "20",
        {
            ATTR_DEVICE_CLASS: SensorDeviceClass.TEMPERATURE,
            ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.CELSIUS,
        },
    )

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.semantic == "temperature"
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
        unit=UnitOfTemperature.CELSIUS,
    )
    hass.states.async_set(entry.entity_id, "20")

    capability = collect_export_capabilities(
        hass,
        instance_id="ha-instance",
    )[0]

    assert capability.semantic == "temperature"
    assert capability.unit == "celsius"


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
