"""Regression tests for the user-facing README structure."""

from pathlib import Path

README = Path(__file__).parents[1] / "README.md"
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
