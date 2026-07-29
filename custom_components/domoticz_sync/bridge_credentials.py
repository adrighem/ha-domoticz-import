"""Persisted credentials for the Home Assistant to Domoticz bridge."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_LINK_ID, CONF_PAIRING_KEY
from .core.protocol import (
    ProtocolError,
    generate_link_id,
    generate_pairing_key,
    validate_link_id,
    validate_pairing_key,
)


@dataclass(frozen=True, slots=True)
class BridgeCredentials:
    """One validated bridge identity and secret."""

    link_id: str
    pairing_key: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject malformed direct construction."""
        validate_link_id(self.link_id)
        validate_pairing_key(self.pairing_key)


def generate_bridge_credentials() -> BridgeCredentials:
    """Generate a complete strong credential pair."""
    return BridgeCredentials(generate_link_id(), generate_pairing_key())


def read_bridge_credentials(entry: ConfigEntry) -> BridgeCredentials:
    """Read and validate credentials without changing the config entry."""
    return BridgeCredentials(
        link_id=entry.data.get(CONF_LINK_ID),
        pairing_key=entry.data.get(CONF_PAIRING_KEY),
    )


@callback
def async_ensure_bridge_credentials(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    version: int | None = None,
    minor_version: int | None = None,
) -> BridgeCredentials:
    """Validate persisted credentials and repair only invalid fields."""
    data = dict(entry.data)

    link_id = data.get(CONF_LINK_ID)
    try:
        validate_link_id(link_id)
    except ProtocolError:
        link_id = generate_link_id()
        data[CONF_LINK_ID] = link_id

    pairing_key = data.get(CONF_PAIRING_KEY)
    try:
        validate_pairing_key(pairing_key)
    except ProtocolError:
        pairing_key = generate_pairing_key()
        data[CONF_PAIRING_KEY] = pairing_key

    credentials = BridgeCredentials(link_id, pairing_key)
    target_version = entry.version if version is None else version
    target_minor_version = (
        entry.minor_version if minor_version is None else minor_version
    )
    if (
        data != entry.data
        or target_version != entry.version
        or target_minor_version != entry.minor_version
    ):
        hass.config_entries.async_update_entry(
            entry,
            data=data,
            version=target_version,
            minor_version=target_minor_version,
        )
    return credentials
