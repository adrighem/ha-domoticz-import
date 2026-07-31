# Plan

## Phase 1: Native Numeric Sensor Profiles

- [x] Task 1.1: Carry Home Assistant state class through the neutral catalog
  - [x] Add focused capability, catalog, and protocol tests
  - [x] Add optional numeric `state_class` metadata
  - [x] Populate it from the Home Assistant entity source
- [x] Task 1.2: Define conservative Domoticz target profiles
  - [x] Verify native type, subtype, options, and value encodings
  - [x] Add profile-selection and value-codec tests
  - [x] Implement native profiles with Custom Sensor fallback
- [x] Task 1.3: Integrate and document native creation and updates
  - [x] Test deterministic creation, adoption, update, and unavailable state
  - [x] Update user and architecture documentation
  - [x] Run current Python, Python 3.9, integration, and Ruff validation
- [x] Task 1.4: Manual verification (`7347ca3`)
  - [x] Install the released version on the test Domoticz instance
  - [x] Confirm a native Temperature target keeps its identity across reconnects
    (live 2026-07-31)
  - [x] Confirm the live fallback uses the expected Custom Sensor profile
  - [x] Confirm a fallback sensor reconnects without duplicates
  - [x] Cover native and fallback reconnects end to end (`2698f1b`)
- [x] Task 1.5: Audit and complete safe native numeric mapping coverage
  (`05a8b5f`)
  - [x] Compare every Home Assistant numeric sensor device class with current
    Domoticz native device profiles
  - [x] Add every missing mapping that preserves meaning, unit, state class,
    and value encoding
  - [x] Document why remaining classes deliberately use Custom Sensor fallback
- [x] Task 1.6: Make directly labelled exclusions visible (`b5b9cdc`)
  - [x] Return structured, side-effect-free exclusion reasons
  - [x] Warn when a directly labelled entity is skipped entirely, with a safe
    reason and entity ID
  - [x] Aggregate or deduplicate diagnostics so reconnects do not create noisy
    logs
  - [x] Test that diagnostics never include entity values or secret material
  - [x] Keep successful Custom Sensor fallbacks out of warning-level logs
    (`05a8b5f`)

## Phase 2: Passive Binary Sensors

- [x] Task 2.1: Define safe binary device-class mappings and fallback behavior
  (`05a8b5f`)
- [x] Task 2.2: Extend the catalog and plugin with passive binary profiles
  (`d5bf19d`)
- [x] Task 2.3: Test creation, adoption, state changes, and unavailable state
  (`d5bf19d`)
- [x] Task 2.4: Completely rewrite the README for both user communities
  (`d5bf19d`)
  - [x] Keep the existing image at the top
  - [x] Give Home Assistant and Domoticz users equally complete setup, upgrade,
    troubleshooting, and verification guidance
  - [x] Explain mappings, fallbacks, limitations, and version compatibility in
    plain language
- [x] Task 2.5: Manually verify representative binary sensors (`cbf30b0`)
  - Live 2026-07-31: a directly labelled Z-Wave motion sensor exported as idx
    `2311`, Type/SubType `244/73`, SwitchType `8`, Unit `1`, and state `Off`;
    a directly labelled no-class Roomba binary sensor exported as idx `2312`,
    Type/SubType `244/73`, fallback SwitchType `0`, Unit `1`, and state `Off`
  - Two unchanged `domoticz.service` restarts under exact plugin and Home
    Assistant `v0.5.0` negotiated binary and numeric v2 features; both targets
    kept the same idx and DeviceID, and remained unique by name and DeviceID

## Phase 3: Protocol Compatibility Hardening

- [x] Task 3.1: Freeze legacy v1 as a heartbeat-only protocol
- [x] Task 3.2: Select v2 through a WebSocket subprotocol and authenticate the
  complete protocol and feature negotiation
- [x] Task 3.3: Gate numeric export behind `ha-export.numeric.v1` and use exact
  schema-versioned application messages and catalog schema v2
- [x] Task 3.4: Test mixed installations, negotiation tampering, downgrade
  boundaries, and current and Python 3.9 compatibility
- [x] Task 3.5: Document and manually verify both mixed-version rolling upgrade
  orders and the matching-tag installation (`d5ab4d4`, `7b44010`)
  - [x] Document both rolling-upgrade orders and matching-tag checks (`621bacc`)
  - [x] Preserve matching-version surfaces with regression coverage (`2698f1b`)
  - [x] Manually verify the Home Assistant-first order (`d5ab4d4`, `7b44010`)
    - Live 2026-07-31: exact plugin `v0.2.0` recreated the v1-only mixed
      checkpoint under HA `v0.3.1`; updating the plugin to `v0.3.1` negotiated
      numeric v2 with the same two persistent targets
    - The current-release replay kept plugin `v0.3.1` while HA moved to
      `v0.5.0`; the mixed session selected only numeric v2 before the plugin
      update
  - [x] Manually verify the Domoticz plugin-first order (`7347ca3`)
    - Live 2026-07-31: HA `v0.2.0` negotiated heartbeat-only v1 with two
      persistent targets; restoring `v0.3.1` negotiated numeric v2 with the
      same two targets
  - [x] Manually verify a matching-tag installation (`d5ab4d4`, `7b44010`)
    - Live 2026-07-31: HACS and the remote plugin checkout both reported exact
      `v0.5.0`; an unchanged `domoticz.service` restart negotiated binary and
      numeric v2 features and preserved both target identities without
      duplicates

## Phase 4: First Export Release

- [x] Task 4.1: Run complete Home Assistant and Domoticz compatibility suites
  (`e5d3575`, `7ac1e0f`)
- [x] Task 4.2: Review setup, security, upgrade, and limitation documentation
  (`e5d3575`)
- [x] Task 4.3: Merge the feature and refresh the Release Please pull request
  (`e5d3575`)
- [x] Task 4.4: Manually validate release artifacts and publish versions 0.3.0
  and 0.3.1 (`e5d3575`, `7d599ae`)
- [x] Task 4.5: Publish numeric mapping coverage and exclusion diagnostics as
  version 0.4.0 (`37763b5`)

## Phase 5: Domoticz Inventory and Drift Repair

- [x] Task 5.1: Design authenticated remote inventory messages (`9b236d7`)
- [x] Task 5.2: Reconcile owned devices against real Domoticz state (`c3ac10e`)
- [x] Task 5.3: Repair safe drift without deleting or claiming unrelated devices
  (`ff075cc`)
- [~] Task 5.4: Test and manually verify restart and manual-change scenarios

## Phase 6: Continuous Synchronization

- [ ] Task 6.1: Define catalog delta and reconnect semantics
- [ ] Task 6.2: Subscribe to selected Home Assistant state and metadata changes
- [ ] Task 6.3: Coalesce updates and recover from disconnects
- [ ] Task 6.4: Test and manually verify live updates and relabeling

## Phase 7: Multi-capability Read-only Entities

- [ ] Task 7.1: Model compound measurements without losing source identity
- [ ] Task 7.2: Add conservative Domoticz compound-device profiles
- [ ] Task 7.3: Test partial availability and atomic updates
- [ ] Task 7.4: Document and manually verify compound devices

## Phase 8: Signed Reverse Commands

- [ ] Task 8.1: Threat-model command authorization, replay, and ownership
- [ ] Task 8.2: Specify signed command and acknowledgement messages
- [ ] Task 8.3: Implement strict validation, idempotency, and audit-safe errors
- [ ] Task 8.4: Test and manually verify the protocol without enabling controls

## Phase 9: Interactive Entities

- [ ] Task 9.1: Select the first safe interactive entity domains
- [ ] Task 9.2: Map Domoticz commands to Home Assistant services
- [ ] Task 9.3: Add permission, feedback, and failure handling
- [ ] Task 9.4: Test, document, and manually verify interactive entities
