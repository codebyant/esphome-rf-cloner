"""Per-command icons, and the precedence rule that protects the user's own choice.

The integration's icon is a default. Home Assistant records it as the entity's *original* icon,
and an icon the user sets on the entity registry record is stored separately and wins. So the
integration can keep offering its icon on every reload without ever overwriting a choice the user
made, and that is what these tests pin down.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.rf_cloner.const import (
    CONF_ICON,
    CONF_NAME,
    CONF_TARGET_ID,
    DEFAULT_COMMAND_ICON,
    DOMAIN,
)
from custom_components.rf_cloner.icons_suggested import suggest_icon
from custom_components.rf_cloner.targets import command_icon, meta_for

from .bridge import BRIDGE_ID, FakeBridge, async_add_esphome_entry
from .common import async_create_target, async_edit_command, async_setup_bridge

LIGHT = 4
UNIQUE_ID = f"{BRIDGE_ID}_{LIGHT}"


@pytest.fixture
async def loaded(hass: HomeAssistant):
    """A loaded bridge holding two unassigned commands."""
    bridge = FakeBridge(hass)
    bridge.commands = {2: "probe_2", LIGHT: "light"}
    esphome_entry = async_add_esphome_entry(hass)
    entry = await async_setup_bridge(hass, esphome_entry.entry_id)
    bridge.calls.clear()
    return bridge, entry


def _entity_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, UNIQUE_ID)
    assert entity_id is not None
    return entity_id


async def test_an_icon_is_stored_and_shown_without_touching_the_bridge(
    hass, loaded
) -> None:
    """Choosing an icon is presentation. It must not be an RF operation."""
    bridge, entry = loaded
    revision_before = bridge.revision

    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: "__unassigned__", CONF_ICON: "mdi:lightbulb"},
    )

    assert meta_for(entry, LIGHT).icon == "mdi:lightbulb"
    assert command_icon(entry, LIGHT) == "mdi:lightbulb"
    assert er.async_get(hass).async_get(_entity_id(hass)).original_icon == "mdi:lightbulb"
    assert hass.states.get(_entity_id(hass)).attributes["icon"] == "mdi:lightbulb"

    assert bridge.mutations == []
    assert bridge.revision == revision_before


async def test_a_user_override_is_never_clobbered(hass, loaded) -> None:
    """The user's icon outranks ours, through reloads and through a target move."""
    _bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    registry = er.async_get(hass)

    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: "__unassigned__", CONF_ICON: "mdi:lightbulb"},
    )
    # The user picks something else on the entity itself.
    registry.async_update_entity(_entity_id(hass), icon="mdi:ceiling-light")
    assert hass.states.get(_entity_id(hass)).attributes["icon"] == "mdi:ceiling-light"

    # The integration keeps offering its own icon: on a reload, and on a move to a target.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(_entity_id(hass)).icon == "mdi:ceiling-light"

    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:power"},
    )

    record = registry.async_get(_entity_id(hass))
    assert record.icon == "mdi:ceiling-light", "the user's icon was overwritten"
    assert record.original_icon == "mdi:power", "the integration's default did not follow"
    assert hass.states.get(_entity_id(hass)).attributes["icon"] == "mdi:ceiling-light"


async def test_an_icon_survives_a_move_and_a_reload(hass, loaded) -> None:
    """An icon belongs to the command, not to the target it happens to sit on."""
    _bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")

    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )
    # Back to unassigned, without naming an icon: the stored one is offered as the default and
    # comes straight back.
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: "__unassigned__", CONF_ICON: "mdi:lightbulb"},
    )
    assert meta_for(entry, LIGHT).icon == "mdi:lightbulb"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert meta_for(entry, LIGHT).icon == "mdi:lightbulb"
    assert hass.states.get(_entity_id(hass)).attributes["icon"] == "mdi:lightbulb"


async def test_a_command_with_no_icon_falls_back(hass, loaded) -> None:
    """Never blank: a command nobody has styled still gets a sensible button."""
    _bridge, entry = loaded
    assert meta_for(entry, LIGHT).icon is None
    assert command_icon(entry, LIGHT) == DEFAULT_COMMAND_ICON
    assert hass.states.get(_entity_id(hass)).attributes["icon"] == DEFAULT_COMMAND_ICON


def test_suggestions_prefer_the_name_then_the_type() -> None:
    """The suggestion table, which only ever pre-fills a field the user can change."""
    assert suggest_icon("power", "fan") == "mdi:power"
    assert suggest_icon("light", "fan") == "mdi:lightbulb"
    assert suggest_icon("speed_1", "fan") == "mdi:fan-speed-1"
    assert suggest_icon("speed_up", "fan") == "mdi:fan-plus"
    assert suggest_icon("open", "gate") == "mdi:gate-open"
    assert suggest_icon("close", "gate") == "mdi:gate"
    assert suggest_icon("stop", "gate") == "mdi:stop"
    # A cover reads the same words as a shutter rather than a gate.
    assert suggest_icon("open", "cover") == "mdi:window-shutter-open"
    assert suggest_icon("close", "cover") == "mdi:window-shutter"
    # Nothing recognised: the target's type, then the generic default.
    assert suggest_icon("xyzzy", "tv") == "mdi:television"
    assert suggest_icon("xyzzy", "generic") == DEFAULT_COMMAND_ICON
    # Separators and case are folded, and a substring is not a match.
    assert suggest_icon("Speed-Up", "fan") == "mdi:fan-plus"
    assert suggest_icon("powerful", "generic") == DEFAULT_COMMAND_ICON
