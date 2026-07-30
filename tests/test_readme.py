"""Regression tests for the user-facing README structure."""

from pathlib import Path

README = Path(__file__).parents[1] / "README.md"
PROTOCOL = Path(__file__).parents[1] / "docs" / "protocol.md"
TOP_IMAGE = (
    '<p align="center">\n'
    '  <img src="custom_components/domoticz_sync/brand/icon@2x.png" '
    'alt="Domoticz Sync app icon" width="160">\n'
    "</p>\n"
)


def test_readme_keeps_brand_image_at_the_top() -> None:
    """The existing product image remains the first README content."""
    assert README.read_text(encoding="utf-8").startswith(TOP_IMAGE)


def test_readme_serves_both_sync_directions() -> None:
    """Both Home Assistant and Domoticz users get complete guidance."""
    readme = README.read_text(encoding="utf-8")

    assert "## Use Domoticz Devices in Home Assistant" in readme
    assert "### Verify the import" in readme
    assert "## Use Home Assistant Entities in Domoticz" in readme
    assert "### Install the Domoticz plugin" in readme
    assert "### Pair the Domoticz plugin" in readme
    assert "### Verify the export" in readme
    assert "## Update Both Installations" in readme
    assert "| Domoticz -> Home Assistant |" in readme
    assert "| Home Assistant -> Domoticz |" in readme


def test_readme_recommends_pypluginstore_with_manual_fallback() -> None:
    """Domoticz users see the managed path before manual file installation."""
    readme = README.read_text(encoding="utf-8")
    recommended_install = "#### Install with PyPluginStore (recommended)"
    manual_install = "#### Install manually"
    recommended_update = "#### Update with PyPluginStore (recommended)"
    manual_update = "#### Update a manual installation"

    assert "https://github.com/adrighem/PyPluginStore" in readme
    assert "https://wiki.domoticz.com/Using_Python_plugins" in readme
    assert "`ha-domoticz-sync`" in readme
    assert readme.index(recommended_install) < readme.index(manual_install)
    assert readme.index(recommended_update) < readme.index(manual_update)


def test_upgrade_docs_cover_both_orders_and_matching_tags() -> None:
    """Operators get explicit checks for every supported update sequence."""
    readme = README.read_text(encoding="utf-8")
    protocol = PROTOCOL.read_text(encoding="utf-8")
    order_headings = (
        "#### Home Assistant first",
        "#### Domoticz plugin first",
        "#### Confirm matching tags",
    )

    assert "### Verify a rolling update" in readme
    assert all(heading in readme for heading in order_headings)
    assert "## Rolling Upgrade Verification" in protocol
    assert "Home Assistant first" in protocol
    assert "Domoticz plugin first" in protocol
    assert "matching release tag" in protocol
