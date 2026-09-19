"""Hardware replacement, with the user's organisation on top of it.

A restore rebuilds RF identity: the same bridge_id, the same command ids, the same waveforms. It
knows nothing about targets, and it does not need to - the targets live in the config entry, which
the new hardware never touches. So the test here is that the two halves meet correctly: the
commands come back under the ids they held, and find the targets still waiting for them.

The destructive half of this - wiping real hardware - is exactly what these fixtures exist to
avoid doing to a live bridge.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
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

FOREIGN_ID = "0123456789abcdef0123456789abcdef"
LIGHT = 4


@pytest.fixture
async def organised(hass: HomeAssistant):
    """A bridge whose two commands are already grouped under a target."""
    bridge = FakeBridge(hass)
    bridge.commands = {2: "probe_2", LIGHT: "light"}
    esphome_entry = async_add_esphome_entry(hass)
    entry = await async_setup_bridge(hass, esphome_entry.entry_id)
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    for command_id, icon in ((2, "mdi:power"), (LIGHT, "mdi:lightbulb")):
        await async_edit_command(
            hass,
            entry,
            command_id,
            {
                CONF_NAME: bridge.commands[command_id],
                CONF_TARGET_ID: fan.target_id,
                CONF_ICON: icon,
            },
        )
    bridge.calls.clear()
    return bridge, entry, fan


async def _poll(hass: HomeAssistant, entry) -> None:
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_a_foreign_identity_freezes_reconciliation_and_keeps_the_grouping(
    hass, organised
) -> None:
    """Replacement hardware says nothing about this bridge, so nothing it says is acted on."""
    bridge, entry, fan = organised
    registry = er.async_get(hass)
    entity_ids = {
        command_id: registry.async_get_entity_id(
            "button", DOMAIN, f"{BRIDGE_ID}_{command_id}"
        )
        for command_id in (2, LIGHT)
    }
    snapshot_before = entry.runtime_data.snapshots.snapshot.as_stored()

    # A blank replacement board, reporting an identity of its own.
    bridge.bridge_id = FOREIGN_ID
    bridge.commands = {}
    bridge.revision = 1
    bridge.next_command_id = 1
    await _poll(hass, entry)

    assert entry.runtime_data.reconciler.identity_mismatch

    # Nothing about the organisation moved.
    assert fan.target_id in targets(entry)
    assert assigned_target(entry, LIGHT).target_id == fan.target_id
    assert command_meta(entry)[LIGHT].icon == "mdi:lightbulb"
    for command_id, entity_id in entity_ids.items():
        record = registry.async_get(entity_id)
        assert record is not None, f"command {command_id} lost its entity to a stranger"
        assert record.config_subentry_id == fan.subentry_id

    # And the replica a restore would read from is exactly as it was.
    assert entry.runtime_data.snapshots.snapshot.as_stored() == snapshot_before


async def test_a_restore_reattaches_commands_to_their_original_targets(
    hass, organised
) -> None:
    """The point of the whole design: replacing hardware does not re-do the organising."""
    bridge, entry, fan = organised
    registry = er.async_get(hass)
    entity_ids = {
        command_id: registry.async_get_entity_id(
            "button", DOMAIN, f"{BRIDGE_ID}_{command_id}"
        )
        for command_id in (2, LIGHT)
    }
    record_ids = {
        command_id: registry.async_get(entity_id).id
        for command_id, entity_id in entity_ids.items()
    }
    device_id = registry.async_get(entity_ids[LIGHT]).device_id

    # Replacement hardware appears, is noticed, and is then restored onto - which is what gives
    # it back the original bridge id, the original command ids and the original waveforms.
    bridge.bridge_id = FOREIGN_ID
    bridge.commands = {}
    await _poll(hass, entry)
    assert entry.runtime_data.reconciler.identity_mismatch

    bridge.bridge_id = BRIDGE_ID
    bridge.commands = {2: "probe_2", LIGHT: "light"}
    bridge.revision = 40
    bridge.next_command_id = 5
    await _poll(hass, entry)

    assert not entry.runtime_data.reconciler.identity_mismatch
    for command_id, entity_id in entity_ids.items():
        record = registry.async_get(entity_id)
        assert record is not None
        assert record.id == record_ids[command_id], "the entity was rebuilt, not restored"
        assert record.unique_id == f"{BRIDGE_ID}_{command_id}"
        assert record.config_subentry_id == fan.subentry_id
        assert assigned_target(entry, command_id).target_id == fan.target_id
    assert registry.async_get(entity_ids[LIGHT]).device_id == device_id
    assert command_meta(entry)[LIGHT].icon == "mdi:lightbulb"


async def test_replacement_leaves_exactly_one_device_per_target(hass, organised) -> None:
    """No duplicate target devices on the far side of a replacement."""
    bridge, entry, fan = organised
    device_registry = dr.async_get(hass)
    before = {
        device.id
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    }

    bridge.bridge_id = FOREIGN_ID
    bridge.commands = {}
    await _poll(hass, entry)
    bridge.bridge_id = BRIDGE_ID
    bridge.commands = {2: "probe_2", LIGHT: "light"}
    await _poll(hass, entry)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = {
        device.id
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    }
    assert after == before, "replacement created or dropped devices"
    assert len(targets(entry)) == 1
    matching = [
        device
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        if device.config_subentry_id == fan.subentry_id
    ]
    assert len(matching) == 1
