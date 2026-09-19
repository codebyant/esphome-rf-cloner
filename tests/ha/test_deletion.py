"""Deleting a target, and deleting a command - which are emphatically not the same thing.

0.1's subentries were commands, so removing one deleted RF from the bridge. 0.2's subentries are
targets, so removing one must only ungroup. That change of meaning is the sharpest edge in this
release, and these tests are what hold it in place.

Home Assistant removes a subentry's devices and entity registry records before any listener of
ours can run, so the target's commands do come off the registry for a moment. What matters is
that they come straight back, as themselves, with the bridge untouched.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
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

PROBE = 2
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


def _entity_id(hass: HomeAssistant) -> str | None:
    return er.async_get(hass).async_get_entity_id("button", DOMAIN, UNIQUE_ID)


async def test_deleting_a_target_preserves_everything_about_its_commands(
    hass, loaded
) -> None:
    """The full regression for the one path that goes through Home Assistant's destructive
    subentry cascade and back.

    Removing a subentry makes Home Assistant delete its devices and its entity registry records
    outright, and no hook runs in between - update listeners are scheduled after the clear. So the
    commands really are removed for an instant and re-materialised from Home Assistant's deleted
    record. Everything a user could have attached to those entities has to come back with them,
    and every one of those things is asserted here rather than sampled, because the cascade is the
    single most destructive thing this integration relies on.

    Two commands, deliberately different: one carries a native user icon override, the other does
    not, so the precedence rule is checked in both directions at once.
    """
    bridge, entry = loaded
    areas = ar.async_get(hass)
    study = areas.async_get_or_create("Study")
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    registry = er.async_get(hass)

    for command_id, icon in ((PROBE, "mdi:power"), (LIGHT, "mdi:lightbulb")):
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

    entity_ids = {
        command_id: registry.async_get_entity_id(
            "button", DOMAIN, f"{BRIDGE_ID}_{command_id}"
        )
        for command_id in (PROBE, LIGHT)
    }
    # Customisations of the kind a real installation accumulates: an area set on the entity
    # itself rather than inherited from the device, a renamed entity, and - on one command only -
    # an icon the user chose in Home Assistant's own entity settings.
    registry.async_update_entity(entity_ids[PROBE], area_id=study.id)
    registry.async_update_entity(entity_ids[PROBE], name="Fan power")
    registry.async_update_entity(entity_ids[LIGHT], icon="mdi:ceiling-light")

    before = {
        command_id: registry.async_get(entity_id)
        for command_id, entity_id in entity_ids.items()
    }
    revision_before = bridge.revision
    commands_before = dict(bridge.commands)
    bridge.calls.clear()

    # Exactly the path the UI takes when the user deletes the RF device.
    hass.config_entries.async_remove_subentry(entry, fan.subentry_id)
    await hass.async_block_till_done()

    # 1. the RF target, its subentry and its device are gone
    assert targets(entry) == {}
    assert fan.subentry_id not in entry.subentries
    assert (
        dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, f"{BRIDGE_ID}_target_{fan.target_id}"), entry.entry_id
        )
        is None
    )

    # 2. + 3. the bridge still holds both commands, and was never asked to change anything
    assert bridge.commands == commands_before
    assert bridge.revision == revision_before
    assert bridge.mutations == [], "removing a target issued an RF mutation"
    assert bridge.calls == [], "removing a target talked to the bridge at all"

    for command_id, entity_id in entity_ids.items():
        after = registry.async_get(entity_id)
        # 4. re-materialised, and unassigned
        assert after is not None, f"command {command_id} did not come back"
        assert assigned_target(entry, command_id) is None
        assert after.config_subentry_id is None
        assert after.device_id is None
        # 5. + 6. + 7. identity is untouched, on both sides
        assert after.unique_id == f"{BRIDGE_ID}_{command_id}"
        assert after.entity_id == entity_ids[command_id]
        assert after.id == before[command_id].id, "the registry record was replaced"
        assert command_id in bridge.commands
        # the button works again
        assert hass.states.get(entity_id) is not None

    # 8. the entity-level area override survives, and is not inherited from a device that is gone
    assert registry.async_get(entity_ids[PROBE]).area_id == study.id

    # 9. native user customisations survive
    assert registry.async_get(entity_ids[PROBE]).name == "Fan power"
    assert registry.async_get(entity_ids[LIGHT]).icon == "mdi:ceiling-light"
    assert hass.states.get(entity_ids[LIGHT]).attributes["icon"] == "mdi:ceiling-light"

    # 10. the integration's icon metadata survives, and is effective again where the user set none
    assert command_meta(entry)[PROBE].icon == "mdi:power"
    assert command_meta(entry)[LIGHT].icon == "mdi:lightbulb"
    assert hass.states.get(entity_ids[PROBE]).attributes["icon"] == "mdi:power"
    assert registry.async_get(entity_ids[LIGHT]).original_icon == "mdi:lightbulb"

    # 11. a reload from here does not duplicate anything
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    buttons = [
        record
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
        if record.domain == "button"
    ]
    assert len(buttons) == 3, "expected two commands plus Cancel learning"
    assert {record.entity_id for record in buttons} == {
        *entity_ids.values(),
        "button.rf_bridge_cancel_learning",
    }
    for command_id, entity_id in entity_ids.items():
        record = registry.async_get(entity_id)
        assert record.id == before[command_id].id
        assert record.config_subentry_id is None
        assert assigned_target(entry, command_id) is None
    assert registry.async_get(entity_ids[PROBE]).area_id == study.id
    assert registry.async_get(entity_ids[LIGHT]).icon == "mdi:ceiling-light"
    assert bridge.mutations == []


async def test_deleting_a_target_only_ungroups_its_commands(hass, loaded) -> None:
    """The headline behaviour: organisation goes, RF stays."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )
    registry = er.async_get(hass)
    entity_id = _entity_id(hass)
    before = registry.async_get(entity_id)
    assert before.config_subentry_id == fan.subentry_id
    revision_before = bridge.revision

    hass.config_entries.async_remove_subentry(entry, fan.subentry_id)
    await hass.async_block_till_done()

    # The target is gone, and so is its device.
    assert targets(entry) == {}
    assert (
        dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, f"{BRIDGE_ID}_target_{fan.target_id}"), entry.entry_id
        )
        is None
    )

    # The command is not.
    assert bridge.mutations == [], "removing a target deleted RF from the bridge"
    assert bridge.revision == revision_before
    assert bridge.commands == {2: "probe_2", LIGHT: "light"}

    after = registry.async_get(entity_id)
    assert after is not None, "the command's entity did not come back"
    assert after.unique_id == UNIQUE_ID
    assert after.config_subentry_id is None
    assert after.device_id is None
    assert assigned_target(entry, LIGHT) is None
    assert hass.states.get(entity_id) is not None


async def test_a_deleted_targets_commands_keep_their_icons(hass, loaded) -> None:
    """The icon belongs to the command, so it cannot go down with the target."""
    _bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )

    hass.config_entries.async_remove_subentry(entry, fan.subentry_id)
    await hass.async_block_till_done()

    assert command_meta(entry)[LIGHT].icon == "mdi:lightbulb"
    assert command_meta(entry)[LIGHT].target_id is None
    assert hass.states.get(_entity_id(hass)).attributes["icon"] == "mdi:lightbulb"


async def test_the_recovered_entity_survives_a_reload(hass, loaded) -> None:
    """Not just recovered in memory: recovered in the registry, for good."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    await async_edit_command(
        hass,
        entry,
        LIGHT,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )
    entity_id = _entity_id(hass)

    hass.config_entries.async_remove_subentry(entry, fan.subentry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    record = er.async_get(hass).async_get(entity_id)
    assert record is not None
    assert record.config_subentry_id is None
    assert assigned_target(entry, LIGHT) is None
    assert bridge.commands == {2: "probe_2", LIGHT: "light"}


async def test_deleting_a_command_is_explicit_and_does_reach_the_bridge(
    hass, loaded
) -> None:
    """The destructive path, which exists and is confirmed separately."""
    bridge, entry = loaded

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_command"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"command_id": str(LIGHT)}
    )
    # Ticking the box only leads to a confirmation; it does not delete.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "light",
            CONF_TARGET_ID: "__unassigned__",
            CONF_ICON: "mdi:lightbulb",
            "delete": True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_delete"
    assert bridge.mutations == [], "the tick box deleted without confirmation"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "command_deleted"

    assert bridge.mutations == ["delete_id"]
    assert LIGHT not in bridge.commands
    assert _entity_id(hass) is None, "the deleted command kept its entity"
    assert command_meta(entry) == {}, "the deleted command kept its metadata"
