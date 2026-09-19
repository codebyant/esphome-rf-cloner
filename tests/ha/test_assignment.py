"""Assigning commands to targets, moving them, and unassigning them again.

Every one of these is Home Assistant-side organisation. The command keeps its id, its unique id,
its entity id, its waveform and its place in the bridge's revision sequence throughout, and the
snapshot the bridge could be restored from does not change. The tests assert all of that rather
than only that the button ended up in the right place, because the identity is the part users
build automations on.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.rf_cloner.const import (
    CONF_ICON,
    CONF_NAME,
    CONF_TARGET_ID,
    DOMAIN,
)
from custom_components.rf_cloner.targets import assigned_target, meta_for

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


def _record(hass: HomeAssistant) -> er.RegistryEntry:
    """The light command's entity registry record."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, UNIQUE_ID)
    assert entity_id is not None
    return registry.async_get(entity_id)


async def _snapshot(entry) -> dict:
    """The bridge's restorable replica, as it would be exported."""
    manager = entry.runtime_data.snapshots
    assert manager.snapshot is not None
    return manager.snapshot.as_stored()


async def test_assign_move_and_unassign_keep_the_command_identical(
    hass, loaded
) -> None:
    """The whole journey, asserting identity at every stop."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    gate = await async_create_target(hass, entry, "Garage Gate", "gate")
    registry = dr.async_get(hass)

    original = _record(hass)
    original_entity_id = original.entity_id
    original_record_id = original.id
    revision_before = bridge.revision
    snapshot_before = await _snapshot(entry)

    # Unassigned to start with: no subentry, no device.
    assert original.config_subentry_id is None
    assert original.device_id is None

    for target in (fan, gate, None):
        result = await async_edit_command(
            hass,
            entry,
            LIGHT,
            {
                CONF_NAME: "light",
                CONF_TARGET_ID: target.target_id if target else "__unassigned__",
                CONF_ICON: "mdi:lightbulb",
            },
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "command_saved", result

        record = _record(hass)
        assert record.entity_id == original_entity_id, "the entity id moved"
        assert record.id == original_record_id, "the registry record was replaced"
        assert record.unique_id == UNIQUE_ID

        if target is None:
            assert record.config_subentry_id is None
            assert record.device_id is None
            assert assigned_target(entry, LIGHT) is None
        else:
            device = registry.async_get_device_by_identifier(
                (DOMAIN, f"{BRIDGE_ID}_target_{target.target_id}"), entry.entry_id
            )
            assert record.config_subentry_id == target.subentry_id
            assert record.device_id == device.id
            assert assigned_target(entry, LIGHT).target_id == target.target_id

    # Nothing about the RF side moved at any point.
    assert bridge.mutations == [], "an assignment reached the bridge"
    assert bridge.revision == revision_before
    assert bridge.commands == {2: "probe_2", LIGHT: "light"}
    assert await _snapshot(entry) == snapshot_before, "the snapshot was rewritten"


async def test_the_other_commands_are_left_alone(hass, loaded) -> None:
    """Assigning one command must not disturb its neighbours."""
    _bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    registry = er.async_get(hass)
    other_id = registry.async_get_entity_id("button", DOMAIN, f"{BRIDGE_ID}_2")
    before = registry.async_get(other_id)

    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )

    after = registry.async_get(other_id)
    assert after.id == before.id
    assert after.config_subentry_id is None
    assert after.device_id is None
    assert assigned_target(entry, 2) is None


async def test_assignment_survives_a_reload(hass, loaded) -> None:
    """Organisation lives in the config entry, so a restart has to bring it back."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )
    before = _record(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    after = _record(hass)
    assert after.entity_id == before.entity_id
    assert after.id == before.id
    assert after.config_subentry_id == fan.subentry_id
    assert after.device_id == before.device_id
    assert meta_for(entry, LIGHT).icon == "mdi:lightbulb"
    assert bridge.mutations == []


async def test_renaming_a_command_is_the_one_thing_that_reaches_the_bridge(
    hass, loaded
) -> None:
    """The name is stored on the device, so it is the exception that proves the rule."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    before = _record(hass)

    result = await async_edit_command(
        hass,
        entry,
        LIGHT,
        {
            CONF_NAME: "ceiling",
            CONF_TARGET_ID: fan.target_id,
            CONF_ICON: "mdi:lightbulb",
        },
    )
    assert result["reason"] == "command_saved"

    assert bridge.mutations == ["rename"]
    assert bridge.commands[LIGHT] == "ceiling"
    # A rename changes the label, never the identity.
    after = _record(hass)
    assert after.entity_id == before.entity_id
    assert after.unique_id == UNIQUE_ID
    assert assigned_target(entry, LIGHT).target_id == fan.target_id
