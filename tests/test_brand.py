"""Tests for Home Assistant brand assets."""

import struct
from pathlib import Path

BRAND_DIR = (
    Path(__file__).parents[1] / "custom_components" / "domoticz_sync" / "brand"
)
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_BRAND_ASSET_SIZE = 1024 * 1024


def _png_size(path: Path) -> tuple[int, int]:
    """Return the dimensions stored in a PNG IHDR chunk."""
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE)
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_brand_icon_dimensions() -> None:
    """Brand icons follow the Home Assistant normal and HiDPI sizes."""
    assert _png_size(BRAND_DIR / "icon.png") == (256, 256)
    assert _png_size(BRAND_DIR / "icon@2x.png") == (512, 512)


def test_brand_icons_are_small_enough_for_hacs() -> None:
    """Brand icons stay within the proposed HACS asset size limit."""
    for filename in ("icon.png", "icon@2x.png"):
        assert (BRAND_DIR / filename).stat().st_size <= MAX_BRAND_ASSET_SIZE
