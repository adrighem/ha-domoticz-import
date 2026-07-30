# Product Guidelines

## User Experience

- Use plain language in setup screens and documentation.
- Make opt-in behavior visible and predictable.
- Keep configuration choices editable while treating generated credentials as
  read-only values.
- Explain limitations at the point where users encounter them.
- Treat Home Assistant and Domoticz users as equal first-class audiences in
  installation, configuration, upgrade, troubleshooting, and verification
  documentation.

## Safety

- Export only entities carrying the configured label directly.
- Use deterministic identities so reconnects do not create duplicates.
- Never delete a Domoticz device automatically.
- Fail closed on malformed, unauthenticated, or stale protocol messages.
- Treat protocol authentication and transport confidentiality as separate
  concerns; document the limits of both WS and Domoticz's native WSS transport.
- Prefer a truthful Custom Sensor over an incorrect native device type.
- Keep imported and exported entities read-only until an authenticated command
  protocol is designed and implemented.

## Compatibility

- Keep the shared core and root-level Domoticz plugin compatible with Python
  3.9 or newer.
- Keep the Home Assistant integration aligned with the repository's supported
  Home Assistant test version.
- Freeze released protocol and message schemas instead of extending them in
  place.
- Keep legacy protocol v1 heartbeat-only and never add write behavior to it.
- Select new major protocols through explicit WebSocket subprotocols, then
  authenticate the complete protocol and feature negotiation.
- Version application features and their exact schemas independently so mixed
  installations use only their common feature set.
- Make version mismatches disable export or the connection rather than silently
  downgrading to a write-capable legacy path.
- Treat both halves as one tested release, recommend matching tags, and
  document the safe rolling-upgrade order.

## Quality

- Add focused tests before implementation where practical.
- Test deterministic creation, adoption, update, and fallback behavior.
- Validate current Python and Python 3.9 before shipping shared-core changes.
- Avoid compatibility branches for behavior that has never shipped or been
  used.
