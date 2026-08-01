<p align="center">
  <img src="custom_components/domoticz_sync/brand/icon@2x.png" alt="Domoticz Sync app icon" width="160">
</p>

# Home Assistant Domoticz Sync

[![CI](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/ci.yml)
[![CodeQL](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/codeql.yml/badge.svg)](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/codeql.yml)

Domoticz Sync helps Home Assistant and Domoticz run side by side. One release
contains a Home Assistant custom integration and a Domoticz Python plugin.

| What you want | Direction | How it works |
| --- | --- | --- |
| See Domoticz devices in Home Assistant | Domoticz -> Home Assistant | The Home Assistant integration polls the Domoticz JSON API and creates read-only sensors and binary sensors. |
| See and control Home Assistant entities in Domoticz | Home Assistant -> Domoticz | The optional Domoticz plugin connects to Home Assistant, mirrors entities in Domoticz, and securely forwards Domoticz UI commands back to Home Assistant. |

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

Keep the Home Assistant integration and Domoticz plugin on matching released
versions when possible. The two halves negotiate shared features safely during
a rolling update, but matching versions are the tested combination.

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

The export side is optional. It mirrors selected Home Assistant entities (like numeric sensors, binary sensors, and switches) directly in Domoticz. The state synchronizes continuously in real-time. If bidirectional control is enabled, any changes or switch toggles made in the Domoticz interface are securely verified via HMAC-SHA256 and executed back in Home Assistant!

### Install the Domoticz plugin

#### Install with PyPluginStore (recommended)

[PyPluginStore](https://github.com/adrighem/PyPluginStore) manages Domoticz
Python plugin installation and updates from the Domoticz interface.

If PyPluginStore is not installed yet:

1. In Domoticz, open **Setup** -> **About** and confirm that Python support is
   listed.
2. Follow the
   [PyPluginStore installation guide](https://github.com/adrighem/PyPluginStore#installation).
   A typical Linux installation is:

   ```bash
   cd /path/to/domoticz/plugins
   git clone https://github.com/adrighem/PyPluginStore.git 00-PyPluginStore
   sudo systemctl restart domoticz.service
   ```

3. Go to **Setup** -> **Hardware** and add hardware of type
   **PyPluginStore**.
4. Go to **Setup** -> **Users**, edit your Domoticz user, and enable the
   **Custom** menu.
5. Open **Custom** -> **pypluginstore**.

Install Domoticz Sync from the store:

1. Open **Custom** -> **pypluginstore**.
2. Search for `ha-domoticz-sync`.
3. Select **Install**.
4. Select **Restart Domoticz**, or restart `domoticz.service` manually if the
   store does not have permission to restart it. This restarts
   the service only; the host does not need to be rebooted.
5. Go to **Setup** -> **Hardware** and confirm that
   **Home Assistant Domoticz Sync** is available.

PyPluginStore chooses the managed Release or Git delivery path available in
its registry. A new release can take some time to enter its reviewed release
index. Rolling updates remain safe, and matching released versions remain the
tested setup.

#### Install manually

Use this fallback if PyPluginStore is unavailable or you prefer to manage the
plugin files directly. It follows the
[standard Domoticz Python plugin installation](https://wiki.domoticz.com/Using_Python_plugins).

Install the whole repository as one directory directly below the Domoticz
plugins directory. The root `plugin.py` loads the shared protocol code from
`custom_components/domoticz_sync/core`, so copying only `plugin.py` is not
enough.

Install a matching tagged release:

<!-- x-release-please-start-version -->
```bash
cd /path/to/domoticz/plugins
git clone --branch v0.6.0 https://github.com/adrighem/ha-domoticz-sync.git
chmod +x ha-domoticz-sync/plugin.py
```
<!-- x-release-please-end -->

You can also extract the complete matching release archive into a single
directory such as
`/path/to/domoticz/plugins/ha-domoticz-sync`.

Restart the Domoticz service after installing the plugin. The whole system does
not need to be restarted.

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
unlocked. If bidirectional control is enabled, toggling these devices in Domoticz sends secure control commands back to Home Assistant. Otherwise, they act as passive mirrors.

See the
[complete Home Assistant to Domoticz mapping](docs/entity-mapping.md) for
units, fallbacks, and deliberate exclusions.

### Export behavior

A complete export and drift-repair pass runs when the Domoticz plugin connects.
When both peers negotiate `domoticz-inventory.v1`, Home Assistant first requests
a complete, authenticated snapshot of the devices visible to that plugin
hardware. It validates every page before comparing the current labelled
entities, its local ownership records, and the real Domoticz state. An
incomplete, rejected, or invalid inventory causes no export writes or local
catalog changes.

- Stable IDs let a reconnect reuse a matching Domoticz device instead of
  creating a duplicate.
- Inventory does not make every device under the plugin hardware sync-owned.
  Ownership comes only from a local catalog binding to the same deterministic
  DeviceID. An existing populated container must have exactly Unit 1 with no
  sibling units before it can be repaired.
- Safe drift in a catalog-owned target is repaired on reconnect. This includes
  its name, `Used` flag, current value, timeout state, and the managed Custom
  Sensor option. Other native and calibration options are preserved.
- A missing catalog-owned target whose source is still selected is recreated
  with the same DeviceID. Domoticz may assign it a new idx, because idx is not
  the stable identity.
- A selected but unavailable missing target is recreated as timed out with a
  neutral value. Its previous Domoticz value cannot be recovered after deletion.
- A changed Type, SubType, SwitchType, wrong unit layout, or sibling unit is
  refused and left untouched. A remote-only target is also left untouched,
  even when its name, profile, or DeviceID looks familiar.
- Home Assistant never automatically deletes a Domoticz device.
- An existing target is never automatically changed to another Domoticz type.
- An unavailable source keeps its last Domoticz value and marks the parent
  Domoticz device as timed out.
- Domoticz timeout state is runtime-only. The plugin reasserts it on reconnect
  while the Home Assistant source remains unavailable.
- Unsupported directly labelled entities produce a safe, deduplicated warning
  in the Home Assistant log. Successfully exported Custom Sensors do not
  produce warning-level logs.

When both peers also negotiate `ha-export.continuous.v1`, existing sync-owned
targets stay current while the authenticated connection remains open:

- State and availability changes are exported. An unavailable source keeps its
  last value and its Domoticz device is marked timed out.
- Relevant state attributes and entity metadata are watched, including the
  effective name, unit, device class, and state class. Mutable fields update in
  place. A change that would require a different Domoticz Type, SubType, or
  SwitchType is still refused rather than silently retyping the target.
- Removing the direct **Domoticz Export** label marks the existing target
  unavailable and stale. It is not deleted, because deletion could discard
  Domoticz history or user configuration. Adding the label back reuses the same
  deterministic DeviceID and updates that target in place.
- Bursts of related events are coalesced into one pass. Home Assistant reads the
  latest complete source state after the short coalescing window, so temporary
  intermediate values may be skipped but the final state is not lost.
- If a newly labelled or newly exportable source has no catalog binding, Home
  Assistant ends the session before writing. The plugin reconnects, obtains a
  fresh inventory, checks capacity and identity collisions, and creates the
  target only when all safety checks pass. The DeviceID remains deterministic
  across the reconnect.
- A disconnect discards queued dirty hints. The next authenticated session uses
  a fresh Home Assistant snapshot and Domoticz inventory, which recovers the
  latest state without replaying old event data.

Creation is also bounded safely. The bridge considers the remote inventory and
both local catalogs before admitting work against the protocol limits of 512
targets and 1,024 total units. It reserves the target and Unit 1 capacity needed
to recover durable sync-owned records before admitting new sources. The plugin
then recounts the complete live Domoticz shape immediately before each create.
If either budget is full, affected sources remain unchanged or unbound without a
partial apply, while unrelated safe updates can continue. A source blocked by
capacity or an unrelated DeviceID collision does not cause a reconnect loop
while it remains selected. Likewise, an unchanged target action rejected by
Domoticz is not retried for every unrelated Home Assistant event; it is eligible
again when its desired state changes or a new session starts.

Continuous export requires `ha-export.continuous.v1`,
`domoticz-inventory.v1`, and at least one matching numeric or binary export
feature in the authenticated feature intersection. During a rolling update,
peers without continuous export keep the safe connect-time behavior for their
common features. Remote inventory repair also stays disabled until both sides
negotiate `domoticz-inventory.v1`.

### Verify the export

1. Label one representative numeric sensor and one binary sensor.
2. Start or restart the Domoticz service. Restart only the service, not the
   whole host.
3. In the Domoticz log, look for
   `Authenticated Home Assistant connection is ready`.
4. Confirm that the ready status includes `domoticz-inventory.v1`,
   `ha-export.continuous.v1`, and the expected `ha-export.numeric.v1` and
   `ha-export.binary.v1` features.
5. Confirm that both devices appear with the expected names, types, and states.
   Record each DeviceID and idx, the sync-owned device count, and one unrelated
   device as a safety sentinel. Do not record credentials.
6. Change the numeric state and trigger the binary sensor. Confirm that Domoticz
   shows the latest values without another ready message or reconnect, and that
   the DeviceIDs and idx values stay unchanged.
7. Make several quick state or safe name changes. After the coalescing window,
   confirm that Domoticz shows the final state and name without duplicate
   targets.
8. Remove the direct export label from one source. Confirm that its existing
   target remains present but becomes unavailable or timed out. Add the label
   back and confirm the same DeviceID and idx become current again.
9. Restart the Domoticz service during or after another source change. Confirm
   that the plugin reconnects, reports the same feature set, and converges to
   the latest state without a duplicate.
10. On a test installation, change an owned target's type or add a sibling unit.
    Trigger a source change and confirm that the target is refused and left
    untouched, with no replacement or duplicate. Restore it manually afterward.
11. Confirm that the unrelated sentinel was never changed or claimed, then
    reconnect once more and verify stable identities and counts. Keep pairing
    details, private state values, and private URLs out of verification notes.

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

#### Update with PyPluginStore (recommended)

1. Open **Custom** -> **pypluginstore**.
2. Search for `ha-domoticz-sync`, or filter the list to installed plugins.
3. Select **Refresh status** if needed, then select **Update** when an update
   is offered.
4. Select **Restart Domoticz**, or restart `domoticz.service` manually. Only the
   service needs restarting; do not reboot the host.
5. Reopen PyPluginStore and confirm that the plugin no longer reports a
   pending update or restart.

#### Update a manual installation

For a Git checkout, select the matching tag and restart the Domoticz service:

<!-- x-release-please-start-version -->
```bash
cd /path/to/domoticz/plugins/ha-domoticz-sync
git fetch --tags
git checkout v0.6.0
# Restart domoticz.service after the update; do not reboot the host.
```
<!-- x-release-please-end -->

For an archive installation, replace the complete plugin directory with the
same tagged release and restart the Domoticz service.

The halves can be updated in either order. They use only features supported by
both sides during the rolling update. Legacy protocol v1 is limited to
authentication and heartbeats, so export remains off if v1 is the only shared
protocol. During a mixed v2 update, common numeric and binary export features
continue, but remote inventory and drift repair stay off until both peers
advertise `domoticz-inventory.v1`. Live updates start only when both peers also
advertise `ha-export.continuous.v1`; otherwise their common export features run
at connect time. The normal Domoticz -> Home Assistant JSON API import is
independent of this plugin negotiation.

Pairing details normally survive an update. Removing and adding the Home
Assistant integration creates new pairing details, which must then be copied
to the Domoticz hardware settings.

### Verify a rolling update

Use a test installation when deliberately exercising mixed versions. Before
each order, note the target release tag and the number of sync-owned Domoticz
devices. Keep Link IDs, pairing keys, and other credentials out of notes and
logs.

#### Home Assistant first

1. Leave the Domoticz plugin on the starting version.
2. Update Home Assistant to the target release and restart Home Assistant.
3. Confirm that the plugin reconnects safely. A v1-only overlap must remain
   heartbeat-only. A v2 overlap may use only the features reported by both
   peers; catalog-only export may continue without inventory repair.
4. Update the Domoticz plugin to the target release and restart the Domoticz
   service only.
5. Confirm that the ready status reports protocol v2 and the expected export
   features, then check that reconnecting did not create duplicate devices.

#### Domoticz plugin first

1. Leave Home Assistant on the starting version.
2. Update the Domoticz plugin to the target release and restart the Domoticz
   service only.
3. Confirm the same safe intermediate behavior: heartbeat-only for v1, or only
   the common negotiated v2 features. Inventory repair must remain off when
   `domoticz-inventory.v1` is not selected.
4. Update Home Assistant to the target release and restart Home Assistant.
5. Confirm protocol v2, the expected features, and an unchanged count of
   existing sync-owned devices after reconnect.

#### Confirm matching tags

1. Confirm that HACS and PyPluginStore report the same release, or that both
   manual installations use the same Git tag or release archive.
2. Confirm that the Domoticz log reports
   `Authenticated Home Assistant connection is ready` with protocol v2 and the
   expected inventory, continuous, numeric, and binary feature names.
3. Verify one native numeric target, one Custom Sensor fallback, and one passive
   binary target when those representative entities are labelled.
4. Reconnect once more and confirm that each source still has exactly one
   Domoticz target.

See the [protocol compatibility contract](docs/protocol.md) for the detailed
mixed-version and feature-negotiation rules.

## Troubleshooting

| Direction | Symptom | What to check |
| --- | --- | --- |
| Domoticz -> Home Assistant | The integration cannot be added | Confirm that Home Assistant can reach the Domoticz URL, the credentials are correct, the URL uses HTTP or HTTPS, and the import SSL setting matches the certificate. Do not put credentials in the URL. |
| Domoticz -> Home Assistant | Expected entities are missing | Check that the Domoticz devices are active and used, visible to the configured user, and allowed by the hidden and favorite options. |
| Domoticz -> Home Assistant | A newly created Domoticz device is missing | Reload the integration so Home Assistant can create entities for the new device or metric. |
| Home Assistant -> Domoticz | PyPluginStore is missing from the Custom menu | Confirm that PyPluginStore was added under Hardware, the current user has the Custom menu enabled, its web folders are writable, and the Domoticz service was restarted. Then hard-refresh the browser. |
| Home Assistant -> Domoticz | The plugin is not listed under Hardware | For PyPluginStore, confirm that `ha-domoticz-sync` is installed, then restart the Domoticz service. For a manual installation, confirm that the complete repository is one direct child of the plugins directory and that its root `plugin.py` is executable. In both cases, confirm Python plugin support under Setup -> About. |
| Home Assistant -> Domoticz | The plugin reports invalid configuration | Enter a host without a scheme or path, a numeric port from 1 to 65535, WS or WSS, and the exact Link ID and Pairing key shown in Home Assistant. |
| Home Assistant -> Domoticz | The plugin cannot connect | Confirm Domoticz can resolve and reach the Home Assistant host and port, the Home Assistant integration entry is loaded, and WS/WSS matches the endpoint. A reverse proxy must forward WebSocket traffic and the `Sec-WebSocket-Protocol` header. |
| Home Assistant -> Domoticz | The connection is ready but export is disabled | Check the reported protocol and features. Install the same release tag on both systems. A v1 compatibility connection is intentionally heartbeat-only. |
| Home Assistant -> Domoticz | A labelled entity is not exported | Put the label directly on a `sensor` or `binary_sensor` and check the Home Assistant log for a fixed exclusion reason. With continuous export, a newly eligible uncataloged source causes one controlled reconnect for fresh inventory. Without `ha-export.continuous.v1`, reconnect the plugin manually. Domoticz-origin mirrors are intentionally skipped. |
| Home Assistant -> Domoticz | A numeric sensor becomes a Custom Sensor | This is the safe fallback when meaning, unit, or state class does not exactly match a native Domoticz type. Check the mapping reference. |
| Home Assistant -> Domoticz | A state, label, or name change is not visible | Confirm that the ready status includes `ha-export.continuous.v1`, `domoticz-inventory.v1`, and the matching numeric or binary feature. Wait for the short coalescing window. If continuous export is not selected, restart or reconnect the plugin to run the connect-time pass. |
| Home Assistant -> Domoticz | A manually deleted target is not recreated | Confirm that the source is still selected, both peers report `domoticz-inventory.v1`, and the target was already recorded as sync-owned. A remote-only DeviceID is deliberately not claimed. Do not edit Home Assistant `.storage` files. |
| Home Assistant -> Domoticz | A retyped target or unexpected unit layout is not repaired | This is deliberate. The bridge never retypes a device or chooses between sibling units. Restore the original profile or remove the conflicting target manually, then reconnect. |
| Home Assistant -> Domoticz | Inventory reconciliation is rejected | Install matching tags and reconnect once. A partial, malformed, oversized, or ambiguous inventory is discarded before any write. Check the sanitized Home Assistant and Domoticz logs without sharing credentials or private state. |

When sharing logs, remove private hosts and URLs. Never include the pairing key,
Domoticz password, tokens, cookies, or other credentials in an issue.

## Current Limitations

- Domoticz -> Home Assistant uses polling. New Domoticz devices or metrics may
  need an integration reload before Home Assistant creates their entities.
- Home Assistant -> Domoticz live updates require the continuous, inventory,
  and matching export-kind features to be selected. Older or mixed versions
  without that intersection retain connect-time-only export.
- The plugin never automatically deletes or retypes Domoticz targets.
- Inventory-aware repair is limited to locally cataloged targets with their
  deterministic DeviceID and exactly Unit 1. Remote-only devices, immutable
  profile changes, and ambiguous unit layouts are left untouched.
- Recreating a deleted unavailable target cannot recover its old Domoticz
  value. The replacement starts with a neutral value and remains timed out.
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
encrypt application data by itself. This includes inventory names, values, and
device metadata:

- `WS` is plaintext. Use it only on a trusted LAN or inside a VPN.
- Native Domoticz `WSS` encrypts traffic against passive observation, but the
  current Domoticz transport does not verify the Home Assistant certificate or
  hostname. It is not sufficient for an untrusted network by itself.

Keep the bridge on a trusted network or VPN and do not expose it directly to
the public internet. Keep pairing keys private and never put them in URLs,
logs, diagnostics, or issue reports.

Continuous change notifications retain only a session-local dirty marker. The
bridge reads current state after coalescing and does not retain event payloads
for replay. Logs report fixed diagnostics and aggregate outcomes; verification
notes and shared logs must still exclude credentials, private URLs, and private
state values.

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
