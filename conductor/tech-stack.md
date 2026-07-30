# Tech Stack

## Runtime

- Python 3.14.2 or newer for the Home Assistant custom integration.
- Python 3.9 or newer for the shared neutral core and root-level Domoticz
  plugin.
- Home Assistant custom component APIs.
- Domoticz Python plugin API.

## Libraries

- `aiohttp` for Home Assistant-side HTTP and WebSocket handling.
- `voluptuous` for Home Assistant configuration schemas.
- Standard-library-only code where practical in the neutral core and Domoticz
  plugin.

## Testing and Quality

- `pytest` and `pytest-asyncio`.
- `pytest-homeassistant-custom-component` for Home Assistant compatibility.
- Ruff for linting and formatting.
- GitHub Actions, CodeQL, dependency review, and pip-audit.

## Delivery

- GitHub pull requests.
- Conventional Commits.
- Release Please for tagged releases and changelogs.
- One repository and release artifact containing both the Home Assistant
  integration and root-level `plugin.py`.
