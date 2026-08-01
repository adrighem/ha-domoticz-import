"""Constants for the Domoticz Sync integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "domoticz_sync"
CONFIG_ENTRY_VERSION = 2
CONFIG_ENTRY_MINOR_VERSION = 1

CONF_INCLUDE_HIDDEN = "include_hidden"
CONF_FAVORITE_ONLY = "favorite_only"
CONF_EXPORT_LABEL_ID = "export_label_id"
CONF_LINK_ID = "link_id"
CONF_PAIRING_KEY = "pairing_key"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_VERIFY_SSL = "verify_ssl"

DATA_BRIDGE_MANAGER = "_bridge_manager"
DATA_EXPORT_LABEL_ID = "_export_label_id"
EXPORT_LABEL_ID = "domoticz_export"
EXPORT_LABEL_NAME = "Domoticz Export"

CONTROLLABLE_EXPORT_DOMAINS = frozenset({"input_boolean", "switch"})

DEFAULT_NAME = "Domoticz Sync"
DEFAULT_SCAN_INTERVAL = 60
MIN_SCAN_INTERVAL = 10
DEFAULT_URL = "http://localhost:8080"
DEFAULT_VERIFY_SSL = True

DEFAULT_UPDATE_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
