<p align="center">
  <img src="custom_components/domoticz_sync/brand/icon@2x.png" alt="Domoticz Sync app icon" width="160">
</p>

# Domoticz Sync for Home Assistant

[![CI](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/ci.yml)
[![CodeQL](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/codeql.yml/badge.svg)](https://github.com/adrighem/ha-domoticz-sync/actions/workflows/codeql.yml)

This custom integration syncs device state from Domoticz into Home Assistant using the Domoticz JSON API.

It is intentionally read-only for the first version:

- Polls `/json.htm?type=command&param=getdevices&filter=all&used=true&order=Name`
- Creates Home Assistant sensors for typed Domoticz values such as temperature, humidity, pressure, power, counters, rain, wind, battery, and text values
- Creates read-only binary sensors for Domoticz switch/security states such as motion, door contact, smoke, moisture, and on/off switches
- Supports username/password Basic Auth
- Supports hidden-device and favorite-only filters through the options flow

## Installation

This integration is installed via **HACS (Home Assistant Community Store)** as a custom repository:

[![Open your Home Assistant instance and add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=adrighem&repository=ha-domoticz-sync&category=integration)

Alternatively, add it manually:

1. Open **HACS** in your Home Assistant interface.
2. Click the three dots `...` in the top-right corner and select **Custom repositories**.
3. Paste the URL of this repository (`https://github.com/adrighem/ha-domoticz-sync`) in the **Repository** field.
4. Select **Integration** as the **Category** and click **Add**.
5. Find **Domoticz Sync** in HACS, click **Download** in the bottom-right, and download the latest version.
6. **Restart Home Assistant** to load the custom component.
7. Go to **Settings** -> **Devices & Services** -> **Add Integration**, search for **Domoticz Sync**, and follow the configuration steps.

## Configuration

The setup flow asks for:

- **Domoticz URL**, for example `http://192.168.1.20:8080` or `https://xmpp.vanadrighem.eu:8443/domoticz`
- **Username and password**, if your Domoticz instance requires them
- **SSL verification** for HTTPS Domoticz installations

### Syncing Only Selected Devices (Recommended)

By default, if you log in with an administrator or generic user, **all** active devices in Domoticz will be synced to Home Assistant. 

To limit the sync to a specific list of devices, we highly recommend creating a dedicated user in Domoticz:
1. In your Domoticz web interface, go to **Setup** -> **Users** and add a new user.
2. Select the newly created user in the list and click the **Set Devices** button.
3. Explicitly add only the devices you want to make visible in Home Assistant.
4. During the integration setup in Home Assistant, enter this dedicated user's **Username** and **Password**.

### Options

After setup, you can configure the following options:

- **Include hidden devices**
- **Only import favorite devices**
- **Polling interval in seconds** (how often to fetch updates from Domoticz)

## Development

Run tests from this folder:

```bash
python -m pytest
```

The parser and API client are split from Home Assistant entity code so support for more Domoticz device types can be added with focused tests.

Releases are managed by Release Please. Use Conventional Commit messages such as `fix:` and `feat:` for user-visible changes.

## Security

CodeQL, Dependency Review, pip-audit, and Dependabot are configured for this repository. Please report vulnerabilities privately through GitHub Security Advisories and do not include secrets in public issues.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
