# Protocol Compatibility Contract

This document defines the compatibility rules between the Home Assistant
integration and the Domoticz companion plugin. The two halves are installed and
updated independently, so a safe mixed-version state is part of the protocol,
not an installation accident.

The protocol provides mutual authentication, message integrity, role
separation, replay protection, and strict input validation. It does not itself
encrypt application data. Transport confidentiality is described below.

## Protocol Families

### Legacy v1

Legacy v1 is the protocol shipped before the first Home Assistant-to-Domoticz
export release. It does not use a WebSocket subprotocol token.

Its behavior is permanently limited to:

- mutual authentication and session establishment;
- the fixed empty inventory and ready markers that enter heartbeat mode; and
- signed ping and pong heartbeats.

Legacy v1 never authorizes Home Assistant to create or update a Domoticz
device. Home Assistant may continue accepting a v1 connection from an older
plugin, but it must keep export disabled for that session.

The v1 handshake, envelope, and allowed messages are frozen. New fields,
message types, schemas, and write capabilities must not be added to v1.

### Protocol v2

Protocol v2 is selected during the HTTP WebSocket upgrade with the exact
subprotocol token:

```text
ha-domoticz-sync.v2
```

The plugin sends its ordered protocol offer in `Sec-WebSocket-Protocol`. Home
Assistant selects a supported token and returns it in the upgrade response.
When v2 is not selected, both current implementations may use frozen legacy v1
for authentication and heartbeats, with every export feature disabled.

The HTTP headers provide early selection, but they are not the authenticated
source of truth. The v2 handshake repeats the complete negotiation:

- `client_protocols` contains the ordered client protocol offer;
- `selected_protocol` contains the WebSocket selection;
- `client_features` contains the features supported by the plugin;
- `server_protocols` and `server_features` contain Home Assistant's support;
  and
- `selected_features` is the exact, sorted intersection of both feature lists.

Protocol and feature lists are bounded and duplicate-free. Protocol names use
WebSocket-token syntax; feature IDs use a conservative identifier syntax. The
complete client offer, server offer, protocol selection, and feature selection
are included in the canonical handshake transcript. Both role-specific
authentication proofs, the session key, and the session ID are therefore bound
to the same negotiation. Changing or stripping a repeated value makes
authentication fail.

## Application Features and Schemas

Numeric and passive binary export are independent write-capable features:

```text
ha-export.numeric.v1
ha-export.binary.v1
```

Remote Domoticz inventory is a separate read-only feature:

```text
domoticz-inventory.v1
```

Continuous export is a separate session-lifecycle feature:

```text
ha-export.continuous.v1
```

It adds no application payload schema and does not authorize a numeric or
binary write by itself. It is used only when `domoticz-inventory.v1` and at
least one matching export-kind feature are also selected. An implementation
advertises continuous export only after it implements the complete subscription,
coalescing, session-lifecycle, and recovery behavior below. Merely recognizing
the feature identifier does not advertise it.

An implementation advertises `domoticz-inventory.v1` only when it implements
the complete request, paging, validation, and fail-closed behavior below.
Inventory support does not change either export feature.

Home Assistant may send an export action only when its feature appears in
`selected_features`. A session can support numeric export, binary export, both,
or neither. The Domoticz plugin rejects a feature-bearing message that was not
negotiated.

V2 application messages use exact schemas:

- `application_ready` uses schema `1` and is feature-independent;
- `apply` and `apply_result` use schema `1` and require
  `ha-export.numeric.v1`; and
- `binary_apply` and `binary_apply_result` use schema `1` and require
  `ha-export.binary.v1`; and
- `inventory_request` and `inventory_result` use schema `1` and require
  `domoticz-inventory.v1`.

The numeric schemas remain unchanged by the addition of binary export. Parsers
require the exact keys and value types for the selected message and schema.
Missing fields, extra fields, unknown schemas, unsupported message types,
invalid sequences, cross-kind values, and messages belonging to an unselected
feature fail closed.

An existing schema is immutable after release. Adding an optional field is
still a schema change and requires a new schema or a new versioned feature.
Peers advertise support for the new feature and use it only when it is
selected. Unknown but syntactically valid feature identifiers may be advertised
and are harmless when they are not in the intersection.

## Authenticated Domoticz Inventory

`domoticz-inventory.v1` lets Home Assistant obtain one read-only snapshot of
the targets visible to the connected Domoticz plugin hardware instance. It does
not enumerate other Domoticz hardware, but hardware scope alone is not proof
that a target is owned by this sync. Home Assistant sends the request only after
`application_ready`, and only when the feature is in `selected_features`.

The request is the exact schema below. `request_id` follows the existing
non-empty application identifier rules and correlates every result page with
this request.

```json
{
  "schema": 1,
  "type": "inventory_request",
  "request_id": "inventory-1"
}
```

The plugin takes one immutable snapshot after accepting the request, sorts its
targets by `target_id` and each target's units by `unit`, and returns consecutive
one-based pages. Every page has this exact top-level schema:

```json
{
  "schema": 1,
  "type": "inventory_result",
  "request_id": "inventory-1",
  "status": "confirmed",
  "page": 1,
  "complete": true,
  "targets": [
    {
      "target_id": "HAEXAMPLEDEVICEID",
      "timed_out": false,
      "units": [
        {
          "unit": 1,
          "type": 244,
          "subtype": 73,
          "switch_type": 8,
          "name": "Hall motion",
          "used": true,
          "n_value": 0,
          "s_value": "Off",
          "custom_option": null,
          "has_other_options": false
        }
      ]
    }
  ]
}
```

The result contains only `schema`, `type`, `request_id`, `status`, `page`,
`complete`, and `targets`. `status` is exactly `confirmed` or `rejected`; `page`
is a positive safe integer; `complete` is a JSON boolean; and `targets` is an
array. The nested schemas are also exact:

- A target contains only `target_id`, `timed_out`, and `units`.
  `target_id` is the non-empty Domoticz `DeviceID`, with no surrounding
  whitespace, used as the protocol's opaque target ID, and is at most 128 UTF-8
  bytes. `timed_out` is a JSON boolean. `units` is an array and may be empty so
  a partially removed device remains observable.
- A unit contains only `unit`, `name`, `type`, `subtype`, `switch_type`, `used`,
  `n_value`, `s_value`, `custom_option`, and `has_other_options`. `unit` is an
  integer from 1 through 255. `type`, `subtype`, and `switch_type` are
  non-negative safe integers. `name` is at most 512 UTF-8 bytes and `s_value`
  is at most 4,096 UTF-8 bytes. `used` and `has_other_options` are JSON
  booleans; `n_value` is a safe integer; and `custom_option` is either the exact
  string value of Domoticz's `Custom` option, bounded to 1,024 UTF-8 bytes, or
  JSON `null` when that key is absent. `has_other_options` is true when the unit
  has any option key other than `Custom`. Arbitrary option names and values are
  deliberately not placed on the wire. The wire booleans normalize Domoticz's
  `0` and `1` flags without changing their meaning.
- `target_id` values are unique in the complete snapshot. Unit numbers are
  unique within their target. A target and all of its units are atomic and are
  never split between pages.

Each `inventory_request` and `inventory_result` is the payload of the existing
v2 signed application envelope. The envelope authenticates the session,
direction, sequence, and complete payload. The request direction is
`home_assistant_to_domoticz`; every result page uses
`domoticz_to_home_assistant`. There is no second inner signature.

### Bounds and completion

One request has one fixed ten-second deadline from sending the request through
receiving its terminal page. A page does not extend that deadline. Only one
inventory request may be in flight on a session. Implementations enforce all of
these limits before accepting the snapshot:

- at most 512 result pages, numbered consecutively from `1`;
- at most 64 targets in one page and 512 targets in the complete snapshot;
- at most 1,024 units across the complete snapshot;
- at most 60 KiB of canonical JSON in each inventory application payload,
  leaving room inside the existing 64 KiB signed-envelope limit; and
- exactly one terminal page with `complete` equal to `true`.

Every page of an accepted snapshot has `status` equal to `confirmed`. Every
non-terminal confirmed page contains at least one target. A terminal confirmed
page also contains at least one target unless it is the complete empty
inventory. The plugin must not truncate a snapshot to meet a bound. If it
cannot take or represent the complete snapshot, including when one atomic
target exceeds the payload limit, it sends only this exact terminal rejection:

```json
{
  "schema": 1,
  "type": "inventory_result",
  "request_id": "inventory-1",
  "status": "rejected",
  "page": 1,
  "complete": true,
  "targets": []
}
```

A rejected result is not an empty inventory and carries no partial targets or
diagnostic text.

Home Assistant stages pages without exposing them to reconciliation. It accepts
the inventory only after the terminal page and after validating the signature,
session, direction, sequence, request ID, status, page order, exact schemas,
uniqueness, bounds, and complete snapshot. Before that terminal page, a
rejection; a missing, duplicate, late, or out-of-order page; a mismatched
request ID; an unknown or extra field; an invalid value; a timeout; a
disconnect; or a limit violation discards the whole staged snapshot. It must
not turn partial data into deletions, adoption, drift repair, or an empty
inventory. The durable target catalogs remain unchanged. The terminal page is
authoritative; any later inventory page is a new session protocol violation and
closes the connection rather than retroactively rolling back confirmed work.

The authoritative empty inventory has one unambiguous form:

```json
{
  "schema": 1,
  "type": "inventory_result",
  "request_id": "inventory-1",
  "status": "confirmed",
  "page": 1,
  "complete": true,
  "targets": []
}
```

This is distinct from legacy v1's frozen `{"type":"inventory","targets":[]}`
lifecycle marker. The legacy marker does not describe real Domoticz state. A
missing feature, missing response, timeout, or disconnect also never means that
the remote inventory is empty.

The feature ID, its application schema, the WebSocket protocol version, and the
Home Assistant catalog schema are independent version domains. Schema `1` under
`domoticz-inventory.v1` is immutable. An incompatible inventory shape requires
a separately negotiated versioned feature and exact parser rather than changing
this schema in place.

This contract authenticates and integrity-protects inventory but does not
encrypt it. Use the WSS, trusted-network, and VPN guidance under Transport
Confidentiality. The inventory feature is read-only and does not itself
authorize deletion, retyping, claiming an unrelated device, or any export
write. The separately negotiated numeric or binary export feature still gates
each repair action.

### Inventory-aware reconciliation

After accepting the complete snapshot, Home Assistant loads both local target
catalogs and validates their deterministic source bindings before the first
write. It then applies these ownership and repair rules independently for each
negotiated export kind:

- A remote target is sync-owned only when an existing local catalog record
  binds the same source to its deterministic `target_id`. A populated remote
  layout must contain exactly Unit 1 with no sibling units. A DeviceID prefix,
  name, idx, or matching profile is not ownership proof.
- A current source without a catalog record may be created only when its
  deterministic DeviceID is absent from the remote snapshot. If that DeviceID
  already exists remotely, it remains remote-only and is not adopted or
  changed.
- A missing or empty catalog-owned target with a current selected source is
  recreated with the same DeviceID. Domoticz may allocate a new idx because idx
  is observational, not identity. A missing stale target is not recreated.
- A catalog-owned Unit 1 with the expected Type, SubType, and SwitchType is
  converged to the source name, an enabled `Used` flag, the encoded state, and
  the timeout state. For Custom Sensors, the bridge manages the `Custom` option
  while preserving unrelated native and calibration options.
- A deleted, selected, unavailable target is recreated with `nValue` equal to
  `0`, an empty `sValue`, and its parent timed out. Its previous Domoticz value
  cannot be recovered after the device was deleted.
- An immutable profile mismatch, a unit other than Unit 1, sibling units, or an
  ambiguous identity is refused and left untouched. No repair deletes or
  retypes a Domoticz target.

Removing an export label therefore cannot delete the previous target. A
catalog-owned target that is no longer present in the current Home Assistant
selection is retained and marked unavailable when its remote layout is safe.

Inventory-aware reconciliation preserves the existing confirmation and local
persistence boundary. Each remote change is re-read and confirmed before its
catalog record is stored. If persistence becomes uncertain, the batch stops;
the deterministic action can be retried safely after reconnect. A rejected or
incomplete inventory never reaches this stage and cannot change a target or
catalog.

When `domoticz-inventory.v1` is not selected, no inventory messages are sent.
Mutually selected numeric and binary features retain the earlier catalog-only
connect-time export behavior, without remote drift detection or repair.

## Continuous Export

`ha-export.continuous.v1` turns selected Home Assistant changes into serialized
reconciliation cycles while one authenticated v2 session remains open.
It deliberately reuses the immutable schema-1 numeric and binary apply
messages. There is no `delta` payload, no mutable catalog revision on the wire,
and still only one complete inventory snapshot per session.

A Home Assistant event is only a value-free dirty hint. After a bounded
coalescing window, Home Assistant reads the current complete labelled source
snapshot, jointly validates both durable target catalogs, and derives the
smallest catalog-owned action delta. Signed envelope sequences order all
application traffic; independent request IDs correlate each apply result.

The subscription covers state and attribute changes for directly labelled
sources, including availability, plus selection and registry metadata that can
change the effective name or exported profile. The callback carries no source
value or event payload. A profile change that would require a different
Domoticz Type, SubType, or SwitchType remains an immutable-layout refusal.

The source snapshot, catalogs, and writes for one destination are serialized as
one cycle, while each confirmed action retains its existing atomic catalog
persistence boundary. Only one apply is in flight across both export kinds;
signed ping and pong heartbeats may interleave. An event that arrives during
collection, apply, or persistence leaves the destination dirty and causes one
follow-up cycle from fresh state. Event bursts may collapse into one cycle
because intermediate Home Assistant values are observations, not an ordered
command stream. The latest authoritative state must not be lost or replaced by
event payload data.

Removing a direct export label retains the catalog-owned Domoticz target and
marks it unavailable and stale; it never sends a delete. Adding the label back
reuses the same source identity and deterministic DeviceID. Updates continue
only for sources already bound by a durable catalog record, and the plugin
rechecks the live target shape immediately before every mutation.

A source newly entering the desired export selection without a durable catalog
record might have been relabelled or might have become exportable after a
state, metadata, or exclusion change. Home Assistant must not persist a pending
create from the session's older inventory snapshot. Instead it closes the
session without performing any part of that live pass. The plugin reconnects
and the normal connect-time cycle takes a fresh inventory, jointly validates
both catalogs, checks deterministic collisions, and only then creates the new
target. Unrelated, ambiguous, retyped, or multi-unit targets retain the
inventory-aware refusal rules.

If that fresh inventory exposes an unrelated deterministic-ID collision, the
source remains uncataloged and becomes the new session's unbound desired
baseline. It does not trigger another reconnect until it leaves the selection
and later re-enters, preventing an incompatible target from causing a reconnect
loop.

The fresh-session create preflight also enforces both complete-inventory limits
across the remote inventory and both durable catalogs: at most 512 targets and
1,024 total units. Existing catalog-owned records reserve the missing target and
Unit 1 capacity required for recovery, even when their export kind is not
selected for that session, before new sources are admitted in stable source
order. The exact limits are allowed; any action that would exceed either budget
is blocked before its apply or catalog save. The plugin independently recounts
the complete live target and unit shape immediately before each create. Other
owned updates remain eligible. A capacity-blocked source follows the same
unbound-baseline rule, so it cannot cause a reconnect loop until it leaves and
later re-enters the selection.

A target-side rejection does not change the catalog. During the same session,
an identical desired action is suppressed on unrelated dirty cycles. A change
to that source's desired capability makes it eligible again, and a new session
may retry it after fresh inventory. This prevents an incompatible target from
being hammered while preserving recovery from transient rejection.

Disconnecting abandons the session-local dirty generation and every unconfirmed
cycle result. An apply timeout, correlation failure, or uncertain catalog
persistence also closes the session for the same recovery path. No event
payload is persisted for replay. A new
authenticated session starts with a complete Home Assistant snapshot and fresh
Domoticz inventory, so the latest state is the recovery barrier. Deterministic
identities, pending-create records, remote confirmation, and atomic catalog
persistence keep retries idempotent.

If continuous export is absent from the authenticated feature intersection,
both peers retain the released connect-time-only behavior even when inventory
and one or both export kinds are available. A continuous feature selected
without inventory is unusable and fails closed rather than authorizing a live
write from stale remote state.

## Signed Reverse Commands

`domoticz-control.v1` allows the connected Domoticz companion plugin to securely transmit control commands (e.g., in response to user actions in the Domoticz UI) to control the state of Home Assistant entities that are mapped to exported targets.

### Threat Model

1. **Command Authorization & Integrity:** To prevent unauthorized or forged commands, every command message must be cryptographically signed using the session key, packed within a `VerifiedEnvelope` envelope, and verified by Home Assistant using HMAC-SHA256 before execution.
2. **Replay Protection:** To prevent attackers from intercepting and replaying valid commands, each control request includes a strict incremental sequence number and nonce/session validation handled by the verified envelope layers.
3. **Target Ownership Scope:** Home Assistant strictly enforces ownership. Domoticz is only authorized to control entities that are explicitly bound to a target device (present in the local Target Catalog). Any command targeting an unbound, private, or unrelated Home Assistant entity is immediately rejected.
4. **Idempotency:** Command correlation via `request_id` ensures that re-transmissions are handled safely and duplicate executions are avoided.
5. **Safe Fail-Closed Errors:** Rejections and failures return detailed, log-safe audit messages without ever leaking any session secrets, pairing keys, or entity attributes.

### Message Schemas

#### Request (`control_request`)

The Domoticz plugin sends a control request of this exact schema when a user interacts with a synchronized device:

```json
{
  "schema": 1,
  "type": "control_request",
  "request_id": "control-1",
  "target_id": "HA123456789...",
  "unit": 1,
  "command": "Set Level",
  "level": 22.5,
  "color": ""
}
```

- `request_id` must be a non-empty, log-safe, unique transaction correlation ID.
- `target_id` must be a valid, non-empty, whitespace-stable deterministic DeviceID.
- `unit` must be a valid integer between `1` and `255`.
- `command` must be a non-empty string.
- `level` must be a finite floating-point number or integer.
- `color` must be a string.

#### Result (`control_result`)

Home Assistant executes the command and returns a sanitized result:

```json
{
  "schema": 1,
  "type": "control_result",
  "request_id": "control-1",
  "status": "confirmed",
  "error": null
}
```

- `status` must be `"confirmed"` (on successful execution) or `"rejected"` (on validation, authorization, or execution failure).
- `error` must be a non-empty string containing a log-safe error message when status is `"rejected"`, or `null` when status is `"confirmed"`.

## Mixed Installations

Home Assistant and the Domoticz plugin may be updated in either order. The
intermediate states are safe:

| Home Assistant | Domoticz plugin | Result |
| --- | --- | --- |
| New, v1 and v2 aware | Old, v1 only | Authenticated v1 heartbeat session; export is disabled |
| Old, v1 only | New, v1 and v2 aware | Authenticated v1 heartbeat session; export is disabled |
| Current, inventory-aware peer | Current, inventory-aware peer | Authenticated v2 session; inventory is confirmed before export and enables safe drift repair for each selected export kind |
| Current binary-aware peer | Earlier numeric-only v2 peer | Authenticated v2 session; numeric export continues and binary export stays disabled |
| Inventory-aware peer | Earlier v2 peer without inventory | Authenticated v2 session; common export features continue and remote inventory and drift repair stay disabled |
| Continuous-aware peer | Earlier v2 peer without continuous export | Authenticated v2 session; common inventory and export features run once at connect, and no live subscription starts |
| Both support v2 but not the same optional feature | Mixed feature support | The v2 session may run its common baseline, but the unsupported feature is not used |

No mixed state permits legacy writes. A mismatch can temporarily stop export,
but cannot cause either side to interpret a new write as an old operation.

Matching release tags remain the recommended installation because they provide
the same tested feature set on both sides. Negotiation makes rolling upgrades
safe; it is not a reason to run unrelated versions indefinitely.

## Rolling Upgrade Verification

Exercise intentional mixed-version states on a test pair. Before each sequence,
select representative directly labelled entities and record the number of
sync-owned Domoticz targets. Do not record Link IDs, pairing keys, or other
credentials.

### Home Assistant first

1. Keep the Domoticz plugin on the starting release and update Home Assistant.
2. After Home Assistant restarts, confirm that the existing plugin reconnects.
3. Verify that a v1-only overlap remains heartbeat-only, or that a v2 overlap
   advertises and uses only the feature intersection.
4. Update the plugin to the target release, restart the Domoticz service only,
   and confirm that protocol v2 selects the expected features.
5. Reconnect again and verify that existing source identities were reused
   without duplicate targets.

### Domoticz plugin first

1. Keep Home Assistant on the starting release and update the Domoticz plugin.
2. After the Domoticz service restarts, confirm the same safe intermediate
   behavior:
   heartbeat-only v1 or only mutually selected v2 features.
3. Update Home Assistant to the target release and restart Home Assistant.
4. Confirm protocol v2 and the expected feature set.
5. Reconnect again and verify that existing source identities were reused
   without duplicate targets.

### Matching release

Finish both sequences on the same matching release tag. Confirm the installed
tag in HACS and PyPluginStore, or in both manual installations. The ready status
must report protocol v2 and the expected inventory, continuous, numeric, and
binary features, including `domoticz-inventory.v1` and
`ha-export.continuous.v1`. Verify a representative native numeric device,
Custom Sensor fallback, and passive binary device, then reconnect once more and
confirm that the target count is unchanged.

## Future Compatibility Rules

Future protocol changes must follow these rules:

1. Legacy v1 remains heartbeat-only and unchanged.
2. A compatible optional capability gets a new schema-versioned feature ID.
   Existing feature identifiers and message schemas are never redefined.
   Before two versions of one feature family are advertised together, both
   peers must implement and test the same deterministic rule that prefers the
   highest mutually supported schema while retaining older parsers.
3. A change to the handshake, authentication, envelope, sequencing, or v2
   baseline gets a new WebSocket subprotocol, such as
   `ha-domoticz-sync.v3`.
4. During a rolling v3 upgrade, both releases advertise v2 and v3 for a
   documented compatibility window. The selected common protocol and complete
   offers are authenticated just as they are in v2.
5. A peer sends only messages belonging to the selected protocol, selected
   features, and exact schema.
6. No common write-capable protocol means no export. A mutually supported safe
   baseline may continue, and a missing optional feature stays disabled.
7. Compatibility tests cover current/current, new Home Assistant with the
   previous plugin, previous Home Assistant with the new plugin, no common
   protocol, feature intersection, and negotiation tampering.
8. Target-profile selection and value encoding released under a feature remain
   stable. A mapping change that would retype an existing Domoticz target
   requires an explicit migration strategy or a new versioned feature.

Support for an older major protocol may be removed only as an explicit
breaking change after its compatibility window. It must not disappear as a
side effect of adding a feature.

## Catalog Versions

The Home Assistant target catalogs are local persistence, not wire-protocol
documents. Numeric and binary state use separate Store keys:

```text
domoticz_sync.target_catalog.{entry_id}.{destination_id}
domoticz_sync.target_catalog.binary.{entry_id}.{destination_id}
```

Keeping the catalogs separate prevents a peer that supports only one feature
from treating the other capability kind as missing. Their versions are
independent of the wire protocol:

- the outer Home Assistant Store container remains version `1`;
- the current inner target catalog uses exact schema version `3`; and
- wire protocol v2 does not imply catalog schema v3, or the reverse.

Catalog schema v2 is the first released shape containing the required
`state_class` field. Schema v3 adds one exact Boolean `pending` field to each
record. In inventory mode, Home Assistant persists a deterministic pending
ownership intent before asking Domoticz to create a new target. A confirmed
apply replaces it with the normal record. If the apply, connection, or final
save is interrupted, the pending record lets a later reconnect retry the same
DeviceID without treating the resulting remote target as unrelated.

Schema v2 records load as schema v3 records with `pending` equal to `false`.
Inner catalog v1 and unknown future versions fail closed and are not
overwritten. No v1 migration or automatic rebuild is provided because export
was unused before this release. Older software that does not understand schema
v3 must likewise leave it untouched.

After the first export release, a catalog schema change requires an explicit,
tested migration or another documented preservation strategy. Older software
must never replace a future catalog with an empty current-version catalog.
The `domoticz-inventory.v1` wire format has its own negotiated feature and exact
application schema rather than reusing the local catalog version. Future remote
inventory formats receive their own independently negotiated version.

## Transport Confidentiality

The pairing-key protocol authenticates both peers and every application
envelope, but signed JSON is not encrypted. Inventory target names, values, and
device metadata are application data under this rule.

- `WS` exposes message contents to anyone who can observe the network. Use it
  only on a trusted LAN or inside a VPN.
- `WSS` protects against passive observation, but the
  [current Domoticz stream configuration](https://github.com/domoticz/domoticz/blob/d9d0d9a65543c61b9d02fddf744e6850a1571c94/hardware/plugins/PluginTransports.cpp#L498-L508)
  does not enforce server certificate or hostname verification. Do not rely on
  native WSS for server identity authentication. Keep the endpoint on a trusted
  network or VPN and do not expose it directly to the public internet.

Pairing keys must never appear in logs, diagnostics, issue reports, URLs, or
transport error messages.

## Downgrade Boundary

WebSocket subprotocol headers are processed before protocol authentication. An
active intermediary can strip or block them and thereby prevent v2 selection.

That attack is limited to availability:

- a missing v2 selection can fall back only to frozen heartbeat-only v1;
- repeated negotiation values are authenticated inside the v2 handshake; and
- frozen legacy v1 has no write-capable feature or application message.

Header stripping can therefore suppress a connection or negotiated export, but
it cannot make Home Assistant perform a legacy write, make the plugin accept a
v2 write as v1, or forge an authenticated application message.
