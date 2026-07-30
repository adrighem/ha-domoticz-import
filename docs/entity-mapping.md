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

Binary sensors are collected but are not exported yet. The planned passive
mapping uses Domoticz General Switch devices and never sends a command back to
Home Assistant.

| Home Assistant device class | Planned Domoticz SwitchType |
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

## Selection Diagnostics

Directly labelled numeric entities that use Custom Sensor are exported
successfully and do not produce warning-level logs. Directly labelled entities
that cannot be exported produce a safe, deduplicated warning containing only
the entity ID and a fixed reason. If the issue is resolved and later recurs,
Home Assistant reports it again.
