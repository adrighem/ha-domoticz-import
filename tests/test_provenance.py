"""Tests for mirror provenance and loopback detection."""

from custom_components.domoticz_sync.const import DOMAIN
from custom_components.domoticz_sync.provenance import (
    ATTR_DOMOTICZ_IDX,
    ATTR_SYNC_ORIGIN,
    ATTR_SYNC_SOURCE_ID,
    ORIGIN_DOMOTICZ,
    domoticz_provenance_attributes,
    is_domoticz_mirror,
)


def test_builds_domoticz_provenance_attributes():
    """Test mirrors expose their origin and stable source identifier."""
    assert domoticz_provenance_attributes("42") == {
        ATTR_DOMOTICZ_IDX: "42",
        ATTR_SYNC_ORIGIN: ORIGIN_DOMOTICZ,
        ATTR_SYNC_SOURCE_ID: "42",
    }


def test_detects_mirror_by_integration_platform():
    """Test the entity registry platform is the primary loopback signal."""
    assert is_domoticz_mirror(platform=DOMAIN, attributes={})


def test_detects_mirror_by_explicit_provenance():
    """Test provenance survives contexts without registry information."""
    assert is_domoticz_mirror(
        platform=None,
        attributes={ATTR_SYNC_ORIGIN: ORIGIN_DOMOTICZ},
    )


def test_detects_existing_mirror_by_legacy_attribute():
    """Test entities created before provenance was added remain excluded."""
    assert is_domoticz_mirror(
        platform=None,
        attributes={ATTR_DOMOTICZ_IDX: "42"},
    )


def test_does_not_classify_unrelated_entity_as_mirror():
    """Test unrelated Home Assistant entities remain export candidates."""
    assert not is_domoticz_mirror(
        platform="mqtt",
        attributes={ATTR_SYNC_ORIGIN: "home_assistant"},
    )
