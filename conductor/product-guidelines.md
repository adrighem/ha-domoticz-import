# Product Guidelines

## User Experience

- Use plain language in setup screens and documentation.
- Make opt-in behavior visible and predictable.
- Keep configuration choices editable while treating generated credentials as
  read-only values.
- Explain limitations at the point where users encounter them.

## Safety

- Export only entities carrying the configured label directly.
- Use deterministic identities so reconnects do not create duplicates.
- Never delete a Domoticz device automatically.
- Fail closed on malformed, unauthenticated, or stale protocol messages.
- Prefer a truthful Custom Sensor over an incorrect native device type.
- Keep imported and exported entities read-only until an authenticated command
  protocol is designed and implemented.

## Compatibility

- Keep the shared core and root-level Domoticz plugin compatible with Python
  3.9 or newer.
- Keep the Home Assistant integration aligned with the repository's supported
  Home Assistant test version.
- Treat both halves as one release and document any required coordinated
  upgrade.

## Quality

- Add focused tests before implementation where practical.
- Test deterministic creation, adoption, update, and fallback behavior.
- Validate current Python and Python 3.9 before shipping shared-core changes.
- Avoid compatibility branches for behavior that has never shipped or been
  used.
