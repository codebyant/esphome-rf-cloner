"""The integration's own icon, served from the integration rather than the brands CDN.

Home Assistant draws an integration's icon from `brands.home-assistant.io`, which only accepts
integrations that are already distributed - so until a pull request lands there, every page that
shows this integration shows `icon not available` instead.

2026.3 added a way out: `homeassistant.components.brands` checks the integration's own `brand`
directory before it goes to the CDN, for custom integrations only. That makes the icon a file in
this repository rather than a dependency on someone else's merge queue.

The contract is unusually easy to break silently, because every part of it is a filename:

- the directory has to be called `brand`, because `Integration.has_branding` is literally
  `"brand" in self._top_level_files` - `brands`, or `assets`, serves nothing and says nothing;
- the file has to be one of `brands.const.ALLOWED_IMAGES`, or the view 404s before it ever looks
  at the disk;
- and the whole thing is skipped for anything that is not a *custom* integration.

Nothing about that surfaces as an error. It surfaces as the placeholder, which is exactly what it
looked like before the file existed. So this asserts on the bytes the HTTP view returns.
"""

from __future__ import annotations

import struct
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

from custom_components.rf_cloner.const import DOMAIN

BRAND_DIR = Path(__file__).resolve().parents[2] / "custom_components/rf_cloner/brand"
ICON = BRAND_DIR / "icon.png"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(raw: bytes) -> tuple[int, int]:
    """The pixel dimensions in a PNG's IHDR, which is always the first chunk."""
    assert raw.startswith(PNG_MAGIC), "not a PNG"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


# The file itself


def test_the_icon_is_where_home_assistant_looks() -> None:
    """`has_branding` is a filename check, so the directory's name is the whole feature."""
    assert BRAND_DIR.is_dir(), f"{BRAND_DIR} is missing"
    assert BRAND_DIR.name == "brand", "the directory must be `brand`, not `brands`"
    assert ICON.is_file(), f"{ICON} is missing"


def test_the_icon_is_a_square_png() -> None:
    """A non-square or non-PNG icon is served happily and then renders wrong."""
    raw = ICON.read_bytes()
    width, height = _png_size(raw)

    assert width == height, f"the icon is {width}x{height}, not square"
    # Home Assistant's largest use of it is the integration page header, which is well under
    # 96 CSS pixels - so this covers that at 2x without shipping a file that is mostly padding.
    assert width >= 192, f"the icon is {width}px, too small for a 2x display"


def test_the_icon_is_an_allowed_name() -> None:
    """The view 404s on any name outside `ALLOWED_IMAGES`, before it touches the disk."""
    from homeassistant.components.brands.const import ALLOWED_IMAGES

    served = {path.name for path in BRAND_DIR.iterdir() if path.is_file()}

    assert served, "the brand directory is empty"
    assert served <= set(ALLOWED_IMAGES), sorted(served - set(ALLOWED_IMAGES))
    # Everything else resolves to this one through `IMAGE_FALLBACKS`, so it is the only file
    # that has to exist - but it does have to.
    assert "icon.png" in served


# What Home Assistant actually serves


@pytest.fixture
async def brands(hass: HomeAssistant):
    """The component that serves brand images."""
    assert await async_setup_component(hass, "brands", {})
    await hass.async_block_till_done()


async def test_home_assistant_serves_our_icon_not_the_placeholder(
    hass: HomeAssistant, brands, hass_client, enable_custom_integrations
) -> None:
    """The bytes on the wire are this repository's file, not the CDN's `icon not available`."""
    client = await hass_client()

    response = await client.get(f"/api/brands/integration/{DOMAIN}/icon.png")

    assert response.status == 200, await response.text()
    body = await response.read()
    assert body == ICON.read_bytes(), "served something other than our icon"
    assert _png_size(body) == _png_size(ICON.read_bytes())


@pytest.mark.parametrize(
    "image",
    ["icon@2x.png", "logo.png", "logo@2x.png", "dark_icon.png", "dark_logo.png"],
)
async def test_the_other_names_fall_back_to_our_icon(
    hass: HomeAssistant, brands, hass_client, enable_custom_integrations, image
) -> None:
    """One file covers every name, through the fallback chain the CDN build also uses.

    This is what makes shipping a single `icon.png` enough: a surface that asks for the dark
    variant, or the logo, or the 2x, gets this icon rather than the placeholder.
    """
    client = await hass_client()

    response = await client.get(f"/api/brands/integration/{DOMAIN}/{image}")

    assert response.status == 200, await response.text()
    assert await response.read() == ICON.read_bytes()
