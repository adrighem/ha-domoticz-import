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

The first write-capable feature is:

```text
ha-export.numeric.v1
```

Home Assistant may send numeric export actions only when this feature appears
in `selected_features`. The Domoticz plugin must reject a feature-bearing
message that was not negotiated.

V2 application messages use exact schemas. The base `application_ready`
message uses schema `1`. The `apply` and `apply_result` messages also use schema
`1` and require the negotiated `ha-export.numeric.v1` feature. Parsers require
the exact keys and value types for the selected message and schema. Missing
fields, extra fields, unknown schemas, unsupported message types, invalid
sequences, and messages belonging to an unselected feature fail closed.

An existing schema is immutable after release. Adding an optional field is
still a schema change and requires a new schema or a new versioned feature.
Peers advertise support for the new feature and use it only when it is
selected. Unknown but syntactically valid feature identifiers may be advertised
and are harmless when they are not in the intersection.

## Mixed Installations

Home Assistant and the Domoticz plugin may be updated in either order. The
intermediate states are safe:

| Home Assistant | Domoticz plugin | Result |
| --- | --- | --- |
| New, v1 and v2 aware | Old, v1 only | Authenticated v1 heartbeat session; export is disabled |
| Old, v1 only | New, v1 and v2 aware | Authenticated v1 heartbeat session; export is disabled |
| New, v1 and v2 aware | New, v2 aware | Authenticated v2 session; numeric export runs when `ha-export.numeric.v1` is selected |
| Both support v2 but not the same optional feature | Mixed feature support | The v2 session may run its common baseline, but the unsupported feature is not used |

No mixed state permits legacy writes. A mismatch can temporarily stop export,
but cannot cause either side to interpret a new write as an old operation.

Matching release tags remain the recommended installation because they provide
the same tested feature set on both sides. Negotiation makes rolling upgrades
safe; it is not a reason to run unrelated versions indefinitely.

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

Support for an older major protocol may be removed only as an explicit
breaking change after its compatibility window. It must not disappear as a
side effect of adding a feature.

## Catalog Versions

The Home Assistant target catalog is local persistence, not a wire-protocol
document. Its versions are independent:

- the outer Home Assistant Store container remains version `1`;
- the current inner target catalog uses exact schema version `2`; and
- wire protocol v2 does not imply catalog schema v2, or the reverse.

Catalog schema v2 is the first shape containing the required `state_class`
field. Inner catalog v1 and unknown future versions fail closed and are not
overwritten. No v1 migration or automatic rebuild is provided because export
was unused before this release.

After the first export release, a catalog schema change requires an explicit,
tested migration or another documented preservation strategy. Older software
must never replace a future catalog with an empty current-version catalog.
Future remote inventory formats receive their own negotiated feature and
schema rather than reusing the local catalog version.

## Transport Confidentiality

The pairing-key protocol authenticates both peers and every application
envelope, but signed JSON is not encrypted.

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

Header stripping can therefore suppress a connection or numeric export, but it
cannot make Home Assistant perform a legacy write, make the plugin accept a v2
write as v1, or forge an authenticated application message.
