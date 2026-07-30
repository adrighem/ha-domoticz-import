# Home Assistant to Domoticz Export Roadmap

## Background

The companion Domoticz plugin authenticates to Home Assistant and receives a
connect-time catalog of directly labelled numeric sensors. Version 0.3.1 uses
conservative native Domoticz profiles with a Custom Sensor fallback. The first
live fallback device was verified to reconnect without duplication.

## Requirements

### Selection

- Export only entities directly assigned the configured Home Assistant export
  label.
- Do not inherit a device label for export.
- Keep unsupported entities out of the exported catalog.
- Log a safe, deduplicated warning when a directly labelled entity cannot be
  exported.
- Do not warn for a numeric entity that is successfully exported as a Custom
  Sensor.

### Identity and Lifecycle

- Derive a deterministic Domoticz DeviceID from the Home Assistant source
  identity.
- Reconnects must adopt or update an existing matching device.
- Never delete Domoticz devices automatically.
- Later inventory support must repair safe drift without taking ownership of
  unrelated devices.

### Native Numeric Sensors

- Carry enough neutral metadata to distinguish Home Assistant measurement,
  total, and total-increasing sensors.
- Select a native Domoticz type only when its meaning and value encoding are
  reliable.
- Use a Custom Sensor fallback for unknown or ambiguous combinations.
- Test creation, adoption, update, unavailable state, and fallback behavior.
- No migration logic is required for previously created Custom Sensors.

### Later Entity Support

- Add passive binary sensors after native numeric sensors.
- Validate and release the first export version before adding remote inventory.
- Add continuous updates only after inventory and drift repair are reliable.
- Model multi-capability read-only entities before designing reverse commands.
- Authenticate and validate reverse commands before enabling interactive
  entities.

## Accepted Implementation Order

1. Native numeric sensors.
2. Export selection diagnostics.
3. Passive binary sensors.
4. Validate and release the first export version.
5. Real Domoticz inventory and drift repair.
6. Continuous synchronization.
7. Multi-capability read-only entities.
8. Signed reverse command protocol.
9. Interactive entities.

## Constraints

- The Home Assistant integration targets Python 3.14.2 or newer.
- The shared core and Domoticz plugin target Python 3.9 or newer.
- `plugin.py` remains at the repository root for straightforward installation.
- The implementation stays in one repository and ships as one release.
- MQTT is outside this roadmap.
- Secrets must never appear in logs or diagnostics.

## Acceptance Criteria

- The roadmap is represented as testable phases in `plan.md`.
- Numeric sensor device class, unit, and state class produce conservative,
  documented Domoticz target profiles.
- Unknown combinations remain functional through a Custom Sensor fallback.
- Directly labelled exclusions produce actionable, log-safe warnings without
  reconnect noise.
- Both current Python and Python 3.9 compatibility suites pass.
- The user can validate the plugin against a real Domoticz and Home Assistant
  installation before phase completion.
