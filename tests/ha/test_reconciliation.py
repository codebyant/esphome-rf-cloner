"""Following the bridge when it changes behind Home Assistant's back.

The device is the authority for which commands exist and what they are called - it can be taught,
renamed or cleared from its own web page or by a script, with Home Assistant only finding out at
the next poll. What Home Assistant owns is the organisation around those commands, and that has
to survive everything the device does short of the command itself disappearing.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.rf_cloner.const import (
    CONF_ICON,
    CONF_NAME,
    CONF_TARGET_ID,
    DOMAIN,
)
from custom_components.rf_cloner.targets import assigned_target, command_meta, targets

from .bridge import BRIDGE_ID, FakeBridge, async_add_esphome_entry
from .common import async_create_target, async_edit_command, async_setup_bridge

LIGHT = 4


@pytest.fixture
async def loaded(hass: HomeAssistant):
    """A loaded bridge holding two unassigned commands."""
    bridge = FakeBridge(hass)
    bridge.commands = {2: "probe_2", LIGHT: "light"}
    esphome_entry = async_add_esphome_entry(hass)
    entry = await async_setup_bridge(hass, esphome_entry.entry_id)
    bridge.calls.clear()
    return bridge, entry


async def _poll(hass: HomeAssistant, entry) -> None:
    """Make the integration read the bridge again, as the poller would."""
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, command_id: int) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{BRIDGE_ID}_{command_id}"
    )


async def test_a_command_learned_on_the_device_arrives_unassigned(hass, loaded) -> None:
    """Home Assistant adopts what the device grew. It does not guess where it belongs."""
    bridge, entry = loaded
    await async_create_target(hass, entry, "Bedroom Fan", "fan")

    new_id = bridge.add("speed_1")
    await _poll(hass, entry)

    entity_id = _entity_id(hass, new_id)
    assert entity_id is not None, "the new command never became an entity"
    record = er.async_get(hass).async_get(entity_id)
    assert record.config_subentry_id is None
    assert record.device_id is None
    assert assigned_target(entry, new_id) is None
    assert hass.states.get(entity_id) is not None


async def test_a_rename_on_the_device_keeps_the_target_and_the_icon(hass, loaded) -> None:
    """A new label is not a new command."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )
    entity_id = _entity_id(hass, LIGHT)
    before = er.async_get(hass).async_get(entity_id)

    # Renamed somewhere else entirely - the bridge's own page, say.
    bridge.commands[LIGHT] = "ceiling"
    bridge.revision += 1
    await _poll(hass, entry)

    after = er.async_get(hass).async_get(entity_id)
    assert after.id == before.id
    assert after.entity_id == entity_id
    assert assigned_target(entry, LIGHT).target_id == fan.target_id
    assert command_meta(entry)[LIGHT].icon == "mdi:lightbulb"
    assert hass.states.get(entity_id).attributes["friendly_name"].endswith("ceiling")


async def test_a_delete_on_the_device_clears_the_assignment(hass, loaded) -> None:
    """The organisation of a command that no longer exists is not worth keeping."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )
    entity_id = _entity_id(hass, LIGHT)

    del bridge.commands[LIGHT]
    bridge.revision += 1
    await _poll(hass, entry)

    assert er.async_get(hass).async_get(entity_id) is None
    assert command_meta(entry) == {}
    # The target itself is not collateral damage.
    assert fan.target_id in targets(entry)


async def test_an_unreadable_bridge_does_not_look_like_an_empty_one(hass, loaded) -> None:
    """A failed poll must never be mistaken for "the user deleted everything"."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )
    entity_id = _entity_id(hass, LIGHT)

    # Mid-restore: the bridge answers, but its listing is not the final one.
    bridge.restore_incomplete = True
    bridge.commands = {}
    await _poll(hass, entry)

    assert er.async_get(hass).async_get(entity_id) is not None, "a mid-restore listing was believed"
    assert assigned_target(entry, LIGHT).target_id == fan.target_id
    assert command_meta(entry)[LIGHT].icon == "mdi:lightbulb"


async def test_repeated_polls_do_not_duplicate_anything(hass, loaded) -> None:
    """Reconciliation is a fixed point, not an accumulator."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )

    registry = er.async_get(hass)
    before = {
        record.entity_id
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    for _ in range(3):
        await _poll(hass, entry)

    after = {
        record.entity_id
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before
    assert len(targets(entry)) == 1
    assert bridge.mutations == []
