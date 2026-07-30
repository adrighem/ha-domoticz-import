<p align="center">
  <img src="custom_components/domoticz_sync/brand/icon@2x.png" alt="Domoticz Sync app icon" width="160">
</p>

# Domoticz Sync

[![CI](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/ci.yml)
[![CodeQL](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/codeql.yml/badge.svg)](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/codeql.yml)

Domoticz Sync helps Home Assistant and Domoticz run side by side. One release
contains a Home Assistant custom integration and a Domoticz Python plugin.

| What you want | Direction | How it works |
| --- | --- | --- |
| See Domoticz devices in Home Assistant | Domoticz -> Home Assistant | The Home Assistant integration polls the Domoticz JSON API and creates read-only sensors and binary sensors. |
| See selected Home Assistant entities in Domoticz | Home Assistant -> Domoticz | The optional Domoticz plugin connects to Home Assistant and creates or updates read-only Domoticz devices when it connects. |

The Home Assistant integration is required for both directions. It imports from
Domoticz and provides the pairing details for the optional Domoticz plugin.
There is no export-only setup flow yet.

## Requirements

- Home Assistant 2026.6.0 or newer.
- A Domoticz instance whose JSON API is reachable from Home Assistant.
- For Home Assistant -> Domoticz export, a Domoticz installation with Python
  plugins and `DomoticzEx` support, running Python 3.9 or newer.
- For export, Domoticz must be able to make an outbound connection to Home
  Assistant.
- A trusted local network or VPN. See [Security](#security) before choosing
  WS or WSS.

Use matching release tags for the Home Assistant and Domoticz installations.
The two halves negotiate shared features safely, but matching versions are the
tested combination.

## Install the Home Assistant Integration

This installation is required whether you want to import Domoticz devices,
export Home Assistant entities, or do both.

### Install with HACS

[![Open your Home Assistant instance and add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=adrighem&repository=ha-domoticz-sync&category=integration)

If the button does not add the repository:

1. Open HACS in Home Assistant.
2. Open the three-dot menu and choose **Custom repositories**.
3. Add `https://github.com/adrighem/ha-domoticz-sync` as an **Integration**.
4. Find **Domoticz Sync** in HACS and download the latest release.
5. Restart Home Assistant.

### Install without HACS

1. Download the release archive for the version you want to install.
2. Copy `custom_components/domoticz_sync` into
   `/config/custom_components/domoticz_sync` in Home Assistant.
3. Restart Home Assistant.

### Add the integration

1. Go to **Settings** -> **Devices & services** -> **Add integration**.
2. Search for **Domoticz Sync**.
3. Enter the Domoticz URL, for example
   `http://192.168.1.20:8080` or
   `https://domoticz.example.local:8443/domoticz`.
4. Enter a Domoticz username and password if the API requires them.
5. Choose whether Home Assistant should verify the Domoticz HTTPS
   certificate.

The SSL setting in this form applies only to outbound JSON API requests from
Home Assistant to Domoticz. It does not control the Domoticz plugin's WSS
connection.

## Use Domoticz Devices in Home Assistant

The import side is read-only. Home Assistant can display Domoticz values, but
it does not send switch or device commands back to Domoticz.

### Choose which Domoticz devices to import

Domoticz returns all active, used devices visible to the configured account.
For predictable access, create a dedicated Domoticz user:

1. In Domoticz, go to **Setup** -> **Users**.
2. Add or select the dedicated user.
3. Choose **Set Devices** and grant access only to the devices you want in
   Home Assistant.
4. Use that account when adding the integration in Home Assistant.

Open **Settings** -> **Devices & services** -> **Domoticz Sync** ->
**Configure** to change:

- **Include hidden devices**
- **Only import favorite devices**
- **Polling interval in seconds**, from 10 to 3600, with a default of 60

Changing these options reloads the integration.

### Imported entity mappings

A single Domoticz device can create several Home Assistant entities when it
contains several values.

| Domoticz data | Home Assistant result |
| --- | --- |
| Temperature, setpoint, humidity, pressure, lux, UV, voltage, and power | Typed sensor entities |
| Counters, rain, rain rate, wind speed, battery, and signal | Sensor or diagnostic sensor entities |
| P1 smart-meter energy and power values | Separate tariff energy and current power sensor entities |
| Safe numeric or text values without a more specific mapping | Generic sensor entities |
| Motion, door/contact, smoke, leak, lock, occupancy, safety, and switch states | Read-only binary sensor entities |

Values that cannot be interpreted safely are skipped rather than guessed.

### Verify the import

1. Open **Settings** -> **Devices & services** -> **Domoticz Sync**.
2. Confirm that the expected devices and entities are listed.
3. Change a value in Domoticz.
4. Confirm that Home Assistant shows the new value after the configured
   polling interval.

If a new Domoticz device or a new metric does not appear, reload the
integration. Existing imported entities continue updating through polling, but
the set of Home Assistant entities is created when the integration loads.

## Use Home Assistant Entities in Domoticz

The export side is optional. It creates passive mirrors in Domoticz for
selected Home Assistant numeric sensors and binary sensors. These targets are
read-only; the plugin refuses Domoticz commands and never sends them to Home
Assistant.

### Install the Domoticz plugin

Install the whole repository as one directory directly below the Domoticz
plugins directory. The root `plugin.py` loads the shared protocol code from
`custom_components/domoticz_sync/core`, so copying only `plugin.py` is not
enough.

Install a matching tagged release:

<!-- x-release-please-start-version -->
```bash
cd /path/to/domoticz/plugins
git clone --branch v0.4.0 https://github.com/adrighem/ha-domoticz-sync.git
```
<!-- x-release-please-end -->

You can also extract the complete matching release archive into a single
directory such as
`/path/to/domoticz/plugins/ha-domoticz-sync`.

Restart Domoticz after installing the plugin.

### Select Home Assistant entities

The integration creates a **Domoticz Export** label in Home Assistant.

1. Go to **Settings** -> **Devices & services** -> **Entities**.
2. Open a numeric sensor or binary sensor.
3. Add **Domoticz Export** under **Labels** and save.

The label must be assigned directly to the entity. A label inherited from its
parent device is not enough. Entities imported from Domoticz are deliberately
excluded so they cannot be exported back to their source.

### Pair the Domoticz plugin

1. In Home Assistant, go to **Settings** -> **Devices & services** ->
   **Domoticz Sync** -> **Configure**.
2. Copy the displayed **Link ID** and **Pairing key**.
3. In Domoticz, go to **Setup** -> **Hardware**.
4. Add **Home Assistant Domoticz Sync**.
5. Complete the fields:

| Domoticz field | Value |
| --- | --- |
| Home Assistant host | A hostname such as `homeassistant.local` or a local IP address. Do not include `http://`, `https://`, a port, or a path. |
| Home Assistant port | Usually `8123` for a direct connection or `443` for a TLS reverse proxy. |
| Connection | `WS` for a trusted local connection or `WSS` for encrypted transport. Read the WSS limitation in [Security](#security). |
| Link ID | The value shown by the Home Assistant integration. |
| Pairing key | The private value shown by the Home Assistant integration. |

The pairing key is generated for this integration entry. It is not a Home
Assistant password or long-lived access token. Pairing-key rotation is not
available yet.

Domoticz initiates the connection to Home Assistant. You do not need to expose
an incoming port on Domoticz, but the configured Home Assistant host and port
must be reachable from Domoticz. The plugin uses
`/api/domoticz_sync/websocket` automatically.

### Exported numeric mappings

The plugin chooses a native Domoticz device type only when the Home Assistant
meaning, unit, and state class are a safe match.

| Home Assistant meaning | Domoticz device type |
| --- | --- |
| Temperature | Temperature |
| Humidity, or another measurement in percent | Humidity or Percentage |
| Atmospheric pressure | Barometer |
| Pressure | Pressure |
| Voltage | Voltage |
| Current | Current |
| Power | Usage |
| Illuminance | Lux |
| Distance | Distance |
| Weight | Weight |
| Sound pressure | Sound Level |
| Irradiance | Solar Radiation |
| Carbon dioxide in ppm | Air Quality |
| Unit `UV index` | UV |

Other finite numeric sensors remain useful as Domoticz Custom Sensors. This
includes totals, counters, energy, AQI, ambiguous volume flow, and meanings or
units without an exact native match. Date, enum, timestamp, uptime, arbitrary
text, and non-finite values are not exported.

### Exported passive binary mappings

| Home Assistant binary sensor | Domoticz SwitchType |
| --- | --- |
| Door or garage door | Door Contact |
| Opening or window | Contact |
| Motion | Motion Sensor |
| Smoke | Smoke Detector |
| Lock | Door Lock Inverted |
| Any other, missing, or future device class | Generic On/Off |

Door Lock Inverted preserves Home Assistant's binary meaning: `on` means
unlocked. All binary targets are passive mirrors. Changing one in Domoticz
does not send a command to Home Assistant.

See the
[complete Home Assistant to Domoticz mapping](docs/entity-mapping.md) for
units, fallbacks, and deliberate exclusions.

### Export behavior

Export runs when the Domoticz plugin connects. Home Assistant compares the
current labelled snapshot with its local record, then asks the plugin to create
or update only the required targets.

- Stable IDs let a reconnect reuse a matching Domoticz device instead of
  creating a duplicate.
- Home Assistant never automatically deletes a Domoticz device.
- An existing target is never automatically changed to another Domoticz type.
- An unavailable source keeps its last Domoticz value and marks the parent
  Domoticz device as timed out.
- Domoticz timeout state is runtime-only. The plugin reasserts it on reconnect
  while the Home Assistant source remains unavailable.
- Unsupported directly labelled entities produce a safe, deduplicated warning
  in the Home Assistant log. Successfully exported Custom Sensors do not
  produce warning-level logs.

There are no live updates yet. Reconnect the plugin, or restart Domoticz, after
changing a selected state, name, label, unit, or device class.

### Verify the export

1. Label one representative numeric sensor and one binary sensor.
2. Start or restart the Domoticz plugin.
3. In the Domoticz log, look for
   `Authenticated Home Assistant connection is ready`.
4. Confirm that both devices appear in Domoticz with the expected names,
   types, and states.
5. Change a source state in Home Assistant and reconnect the plugin to confirm
   the target updates without creating a duplicate.

The ready status also reports the selected protocol and feature names. If it
reports v1 compatibility mode or no export features, update both installations
to matching tags.

## Update Both Installations

The Home Assistant integration and Domoticz plugin are installed separately.
Updating one does not update the other.

### Update Home Assistant

- With HACS, install the new Domoticz Sync release and restart Home Assistant.
- Without HACS, replace `/config/custom_components/domoticz_sync` with the
  folder from the new release and restart Home Assistant.

### Update Domoticz

For a Git checkout, select the matching tag and restart Domoticz:

<!-- x-release-please-start-version -->
```bash
cd /path/to/domoticz/plugins/ha-domoticz-sync
git fetch --tags
git checkout v0.4.0
# Restart Domoticz after the update.
```
<!-- x-release-please-end -->

For an archive installation, replace the complete plugin directory with the
same tagged release and restart Domoticz.

The halves can be updated in either order. They use only features supported by
both sides during the rolling update. Legacy protocol v1 is limited to
authentication and heartbeats, so export remains off if v1 is the only shared
protocol. The normal Domoticz -> Home Assistant JSON API import is independent
of this plugin negotiation.

Pairing details normally survive an update. Removing and adding the Home
Assistant integration creates new pairing details, which must then be copied
to the Domoticz hardware settings.

See the [protocol compatibility contract](docs/protocol.md) for the detailed
mixed-version and feature-negotiation rules.

## Troubleshooting

| Direction | Symptom | What to check |
| --- | --- | --- |
| Domoticz -> Home Assistant | The integration cannot be added | Confirm that Home Assistant can reach the Domoticz URL, the credentials are correct, the URL uses HTTP or HTTPS, and the import SSL setting matches the certificate. Do not put credentials in the URL. |
| Domoticz -> Home Assistant | Expected entities are missing | Check that the Domoticz devices are active and used, visible to the configured user, and allowed by the hidden and favorite options. |
| Domoticz -> Home Assistant | A newly created Domoticz device is missing | Reload the integration so Home Assistant can create entities for the new device or metric. |
| Home Assistant -> Domoticz | The plugin is not listed under Hardware | Confirm the complete repository is one direct child of the Domoticz plugins directory, `plugin.py` is at its root, files are readable by Domoticz, Python plugins are available, and Domoticz was restarted. |
| Home Assistant -> Domoticz | The plugin reports invalid configuration | Enter a host without a scheme or path, a numeric port from 1 to 65535, WS or WSS, and the exact Link ID and Pairing key shown in Home Assistant. |
| Home Assistant -> Domoticz | The plugin cannot connect | Confirm Domoticz can resolve and reach the Home Assistant host and port, the Home Assistant integration entry is loaded, and WS/WSS matches the endpoint. A reverse proxy must forward WebSocket traffic and the `Sec-WebSocket-Protocol` header. |
| Home Assistant -> Domoticz | The connection is ready but export is disabled | Check the reported protocol and features. Install the same release tag on both systems. A v1 compatibility connection is intentionally heartbeat-only. |
| Home Assistant -> Domoticz | A labelled entity is not exported | Put the label directly on a `sensor` or `binary_sensor`, reconnect the plugin, and check the Home Assistant log for a fixed exclusion reason. Domoticz-origin mirrors are intentionally skipped. |
| Home Assistant -> Domoticz | A numeric sensor becomes a Custom Sensor | This is the safe fallback when meaning, unit, or state class does not exactly match a native Domoticz type. Check the mapping reference. |
| Home Assistant -> Domoticz | A state, label, or name change is not visible | Export is connect-time only. Restart or reconnect the plugin. |
| Home Assistant -> Domoticz | A manually deleted or retyped target is not repaired | Remote Domoticz inventory is not implemented yet, so the local record cannot reliably detect every manual change. Do not edit Home Assistant `.storage` files. |

When sharing logs, remove private hosts and URLs. Never include the pairing key,
Domoticz password, tokens, cookies, or other credentials in an issue.

## Current Limitations

- Domoticz -> Home Assistant uses polling. New Domoticz devices or metrics may
  need an integration reload before Home Assistant creates their entities.
- Home Assistant -> Domoticz runs only when the plugin connects. It does not
  subscribe to live Home Assistant state, metadata, or label changes.
- Export has no remote Domoticz inventory yet, so manual deletion and type
  drift are not always detected or repaired.
- The plugin never automatically deletes or retypes Domoticz targets.
- Exported devices are read-only. Reverse commands and interactive entities
  are not implemented.
- Only directly labelled numeric sensors and passive binary sensors are
  exported. Compound and multi-capability devices are not implemented.
- Native numeric type selection is conservative. Other finite numeric values
  use Custom Sensors.
- Only one active Domoticz plugin connection is allowed for each Link ID.
- Pairing-key rotation and an export-only Home Assistant setup flow are not
  available yet.

## Security

Use a dedicated, least-privilege Domoticz account for the JSON API import.
Home Assistant can verify the Domoticz HTTPS certificate when that option is
enabled.

The companion connection uses a generated pairing key for mutual
authentication, replay protection, and signed messages. The protocol does not
encrypt application data by itself:

- `WS` is plaintext. Use it only on a trusted LAN or inside a VPN.
- Native Domoticz `WSS` encrypts traffic against passive observation, but the
  current Domoticz transport does not verify the Home Assistant certificate or
  hostname. It is not sufficient for an untrusted network by itself.

Keep the bridge on a trusted network or VPN and do not expose it directly to
the public internet. Keep pairing keys private and never put them in URLs,
logs, diagnostics, or issue reports.

Please report suspected vulnerabilities privately through GitHub Security
Advisories. See [SECURITY.md](SECURITY.md).

## Compatibility References

- Home Assistant 2026.6.0 is the minimum tested release.
- The shared core and root Domoticz plugin support Python 3.9 and newer.
- Domoticz must provide the Python plugin framework and `DomoticzEx`.
- [Entity mapping details](docs/entity-mapping.md)
- [Protocol and rolling-update contract](docs/protocol.md)

## Development

Run the complete test suite from the repository root:

```bash
python -m pytest
```

The parser, shared capability model, protocol, Home Assistant adapter, and
Domoticz plugin have focused tests so mappings and compatibility can evolve
safely.

Releases are managed by Release Please. Use Conventional Commit messages such
as `fix:` and `feat:` for user-visible changes.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
