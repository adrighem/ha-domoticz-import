# Home Assistant to Domoticz Mapping

The exporter chooses a Domoticz profile only when the Home Assistant meaning,
unit, state class, and Domoticz value encoding agree. A numeric sensor that
does not have a safe native profile remains functional as a Domoticz Custom
Sensor. It is not treated as unmapped.

This matrix covers the 61 `SensorDeviceClass` values and 28
`BinarySensorDeviceClass` values in Home Assistant 2026.7.

## Numeric Sensors

Native profiles require state class `measurement` or no state class. A
different or unsupported unit uses Custom Sensor.

| Home Assistant meaning | Additional condition | Domoticz profile |
| --- | --- | --- |
| `temperature` | Celsius, Fahrenheit, or kelvin | Temperature |
| `humidity` | `%` | Humidity |
| `battery`, `moisture`, `power_factor` | `%` | Percentage |
| Any numeric sensor without a more specific mapping | `%` | Percentage |
| `atmospheric_pressure` | Supported pressure unit | Barometer |
| `pressure` | Supported pressure unit | Pressure |
| `voltage` | Supported voltage unit | Voltage |
| `current` | Supported current unit | Current |
| `power` | Supported power unit | Usage |
| `illuminance` | Lux | Lux |
| `distance` | Supported length unit | Distance |
| `weight` | Supported mass unit | Weight |
| `sound_pressure` | dB or dBA | Sound Level |
| `irradiance` | W/m2 or BTU/(h*ft2) | Solar Radiation |
| `carbon_dioxide` | ppm | Air Quality |
| No device class | Unit `UV index` | UV |

The following numerical device classes deliberately use Custom Sensor:

| Home Assistant device classes | Reason |
| --- | --- |
| `absolute_humidity`, `area`, `blood_glucose_concentration`, `conductivity`, `data_rate`, `data_size`, `duration`, `energy_distance`, `frequency`, `monetary`, `nitrogen_dioxide`, `nitrogen_monoxide`, `nitrous_oxide`, `ozone`, `ph`, `pm1`, `pm10`, `pm25`, `pm4`, `sulphur_dioxide`, `volatile_organic_compounds`, `volatile_organic_compounds_parts` | Domoticz has no single-value native profile with the same meaning and unit. |
| `apparent_power`, `reactive_power`, `reactive_energy` | Domoticz Usage and energy devices describe real power or energy, so using them would change the meaning. |
| `aqi` | Domoticz Air Quality is a CO2 concentration in ppm, not an air-quality index. |
| `energy`, `energy_storage`, `gas`, `precipitation`, `precipitation_intensity`, `volume`, `volume_storage`, `water` | The tempting Domoticz devices are counters, time-integrating devices, or compound devices. They need lifecycle or multi-capability design first. |
| `signal_strength` | dB or dBm signal strength is not a Domoticz Sound Level measurement. |
| `speed`, `wind_direction`, `wind_speed` | A Domoticz Wind device combines direction, speed, gust, temperature, and chill. |
| `temperature_delta` | A temperature difference is not an absolute Domoticz Temperature value and uses different Fahrenheit conversion. |
| `volume_flow_rate` | Home Assistant describes generic liquid, gas, or air flow; Domoticz Waterflow specifically presents water in L/min. |

`power_factor` without a percent unit also uses Custom Sensor because Home
Assistant may represent it as a ratio from 0 to 1. Raw `rpm` values remain
Custom Sensors because rotation does not necessarily describe a fan.

The non-numeric sensor device classes `date`, `enum`, `timestamp`, and `uptime`
are not exported by the numeric feature.

## Passive Binary Sensors

Binary sensors use Domoticz General Switch devices with Type 244 and Subtype
73. They are read-only mirrors: a Domoticz command is refused and never sent
back to Home Assistant.

| Home Assistant device class | Domoticz SwitchType |
| --- | --- |
| `door`, `garage_door` | Door Contact |
| `opening`, `window` | Contact |
| `motion` | Motion Sensor |
| `smoke` | Smoke Detector |
| `lock` | Door Lock Inverted |
| `battery`, `battery_charging`, `carbon_monoxide`, `cold`, `connectivity`, `gas`, `heat`, `light`, `moisture`, `moving`, `occupancy`, `plug`, `power`, `presence`, `problem`, `running`, `safety`, `sound`, `tamper`, `update`, `vibration`, no class, or an unknown future class | Generic On/Off |

Door Lock Inverted preserves Home Assistant's binary meaning: on means
unlocked. The generic fallback avoids misleading mappings such as carbon
monoxide to Smoke Detector or occupancy to Motion Sensor.

Available `on` and `off` states become `On` and `Off`. An unavailable source
keeps its last Domoticz value and is marked timed out. That timeout is runtime
state and is reapplied when the plugin reconnects after a Domoticz service
restart. When `ha-export.continuous.v1` is selected, later state, availability,
metadata, and direct label changes are reconciled while the connection remains
open.

Deterministic identity makes reconnects adopt the same device. A later device
class change that selects a different SwitchType is rejected rather than
silently retyping the existing Domoticz device.
Once released, these mappings are part of the `ha-export.binary.v1`
compatibility contract. A future change that would retype existing devices
needs an explicit migration or a new feature version.

## Inventory-aware drift repair

When `domoticz-inventory.v1` is negotiated, Home Assistant stages and validates
the complete inventory before planning any export action. Ownership comes only
from a local target-catalog binding between the source and its deterministic
DeviceID. Before mutation, the corresponding remote container must be absent,
empty, or contain exactly Unit 1 with no siblings. A matching DeviceID, name,
idx, or profile by itself is never enough to claim a remote-only device.

For a catalog-owned target with the expected Type, SubType, and SwitchType, the
initial pass and each reconnect restore the source name, enable its `Used` flag,
restore its current encoded value and timeout state, and restore the managed
`Custom` option used by Custom Sensors. Continuous export keeps those mutable
source-derived fields current between reconnects. Other native and calibration
options are preserved. If the whole catalog-owned target is missing and its
source is still selected, the bridge recreates its deterministic DeviceID;
Domoticz may assign a different idx. A deleted, selected, unavailable target is
recreated timed out with a neutral value because its previous Domoticz value no
longer exists.

The bridge refuses and leaves untouched an unexpected Type, SubType,
SwitchType, Unit number, or container with sibling units. It also leaves all
remote-only targets untouched. It never deletes or retypes a Domoticz device,
and removing a Home Assistant export label does not delete the existing target.
If the inventory is incomplete, rejected, malformed, or ambiguous, no
inventory-aware repair or catalog change is performed.

## Compound Devices (Multi-capability Entities)

Multi-capability entities are grouped into a single native compound Domoticz device when the sub-capabilities are exported from the same physical device.

| Grouped Home Assistant device classes | Domoticz Type / SubType | sValue Format |
| --- | --- | --- |
| `temperature` and `humidity` | `82 / 1` (Temp + Hum) | `TEMP;HUM;HUM_STATUS` |

If one of the component capabilities becomes temporarily unavailable (e.g., the humidity sensor is offline but the temperature sensor is online), the compound device supports **partial availability**. The bridge safely retains and falls back to the last known value of the unavailable component parsed from the existing Domoticz device's `sValue`, while atomically updating the available components in place.

## Continuous Synchronization

Continuous synchronization is enabled only when the authenticated session
selects `ha-export.continuous.v1`, `domoticz-inventory.v1`, and the matching
numeric or binary export feature. State and relevant attributes, availability,
the effective entity name, mapping metadata, and direct export-label membership
can then update an existing catalog-owned target without a manual reconnect.

Several quick Home Assistant events are coalesced. The bridge reads the latest
complete source snapshot after the short window instead of replaying event
values, so an intermediate value may be skipped but the final state is applied.
Disconnecting drops these value-free dirty hints; the next session starts from
a fresh source snapshot and Domoticz inventory.

Removing the direct export label marks the catalog-owned target unavailable and
stale instead of deleting it. This protects Domoticz history and local user
configuration. Adding the label again reuses the same deterministic DeviceID.
If an entity becomes newly exportable without an existing catalog binding, the
bridge performs one controlled reconnect so creation is checked against fresh
inventory and proceeds only when every safety check passes. The joint preflight
enforces at most 512 targets and 1,024 total units, reserving recovery capacity
for durable sync-owned records before admitting new sources. The plugin recounts
the live shape immediately before creation. Capacity, collision,
immutable-profile, and ambiguous-layout checks remain fail closed. An unchanged
blocked or rejected source is not repeatedly retried for unrelated Home
Assistant events.

## Selection Diagnostics

Directly labelled numeric entities that use Custom Sensor and binary entities
that use generic On/Off are exported successfully and do not produce
warning-level logs. Directly labelled entities that cannot be exported by the
negotiated feature set produce a safe, deduplicated warning containing only the
entity ID and a fixed reason. If the issue is resolved and later recurs, Home
Assistant reports it again.
