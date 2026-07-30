# Product

## Purpose

Domoticz Sync connects Home Assistant and Domoticz from one maintained code
base. The Home Assistant integration imports Domoticz state as read-only
entities. A companion Domoticz plugin makes an outbound authenticated
connection to Home Assistant and exports explicitly selected Home Assistant
entities to Domoticz.

## Users

The project is for home automation users who run both systems and want a
friendly migration or coexistence path without maintaining separate bridges.

## Goals

- Keep installation and pairing understandable for non-developers.
- Require explicit labels before exporting Home Assistant entities.
- Create and update Domoticz devices safely and idempotently.
- Preserve useful native device semantics when the mapping is reliable.
- Fall back to a Custom Sensor rather than inventing misleading semantics.
- Ship matching Home Assistant and Domoticz halves in one tagged release.
- Never expose credentials in logs, diagnostics, or documentation examples.

## Non-goals

- MQTT transport in the current roadmap.
- Unrestricted bidirectional control before a signed command path exists.
- Silent deletion of Domoticz devices.
- Compatibility migrations for the unreleased Home Assistant export feature.
- Exporting every Home Assistant provider or media domain.

## Current Product State

Domoticz to Home Assistant import is established and read-only. The companion
plugin can pair with Home Assistant and perform a connect-time export of
directly labeled numeric sensors. Native numeric mappings, binary sensors,
remote inventory, continuous synchronization, and controls are being added in
that order.
