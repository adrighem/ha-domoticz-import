# Plan

## Phase 1: Native Numeric Sensor Profiles

- [x] Task 1.1: Carry Home Assistant state class through the neutral catalog
  - [x] Add focused capability, catalog, and protocol tests
  - [x] Add optional numeric `state_class` metadata
  - [x] Populate it from the Home Assistant entity source
- [~] Task 1.2: Define conservative Domoticz target profiles
  - [ ] Verify native type, subtype, options, and value encodings
  - [ ] Add profile-selection and value-codec tests
  - [ ] Implement native profiles with Custom Sensor fallback
- [ ] Task 1.3: Integrate and document native creation and updates
  - [ ] Test deterministic creation, adoption, update, and unavailable state
  - [ ] Update user and architecture documentation
  - [ ] Run current Python, Python 3.9, integration, and Ruff validation
- [ ] Task 1.4: Manual verification
  - [ ] Install the branch on the test Domoticz instance
  - [ ] Confirm representative native devices and fallback sensors
  - [ ] Confirm reconnect updates without duplicates

## Phase 2: Passive Binary Sensors

- [ ] Task 2.1: Define safe binary device-class mappings and fallback behavior
- [ ] Task 2.2: Extend the catalog and plugin with passive binary profiles
- [ ] Task 2.3: Test creation, adoption, state changes, and unavailable state
- [ ] Task 2.4: Document and manually verify representative binary sensors

## Phase 3: First Export Release

- [ ] Task 3.1: Run complete Home Assistant and Domoticz compatibility suites
- [ ] Task 3.2: Review setup, security, upgrade, and limitation documentation
- [ ] Task 3.3: Merge the feature and refresh the Release Please pull request
- [ ] Task 3.4: Manually validate release artifacts and publish version 0.3.0

## Phase 4: Domoticz Inventory and Drift Repair

- [ ] Task 4.1: Design authenticated remote inventory messages
- [ ] Task 4.2: Reconcile owned devices against real Domoticz state
- [ ] Task 4.3: Repair safe drift without deleting or claiming unrelated devices
- [ ] Task 4.4: Test and manually verify restart and manual-change scenarios

## Phase 5: Continuous Synchronization

- [ ] Task 5.1: Define catalog delta and reconnect semantics
- [ ] Task 5.2: Subscribe to selected Home Assistant state and metadata changes
- [ ] Task 5.3: Coalesce updates and recover from disconnects
- [ ] Task 5.4: Test and manually verify live updates and relabeling

## Phase 6: Multi-capability Read-only Entities

- [ ] Task 6.1: Model compound measurements without losing source identity
- [ ] Task 6.2: Add conservative Domoticz compound-device profiles
- [ ] Task 6.3: Test partial availability and atomic updates
- [ ] Task 6.4: Document and manually verify compound devices

## Phase 7: Signed Reverse Commands

- [ ] Task 7.1: Threat-model command authorization, replay, and ownership
- [ ] Task 7.2: Specify signed command and acknowledgement messages
- [ ] Task 7.3: Implement strict validation, idempotency, and audit-safe errors
- [ ] Task 7.4: Test and manually verify the protocol without enabling controls

## Phase 8: Interactive Entities

- [ ] Task 8.1: Select the first safe interactive entity domains
- [ ] Task 8.2: Map Domoticz commands to Home Assistant services
- [ ] Task 8.3: Add permission, feedback, and failure handling
- [ ] Task 8.4: Test, document, and manually verify interactive entities
