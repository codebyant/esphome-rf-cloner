"""RF targets: creating them, editing them, and removing them.

The invariant running through all of it is that a target is Home Assistant's idea of a piece of
equipment and nothing more. Creating, renaming, retyping, re-areaing and deleting one are all
organisational, and none of them may reach the bridge.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr
import pytest

from custom_components.rf_cloner.const import (
    CONF_AREA_ID,
    CONF_NAME,
    CONF_TARGET_TYPE,
    DOMAIN,
    SUBENTRY_TYPE_TARGET,
)
from custom_components.rf_cloner.targets import targets

from .bridge import BRIDGE_ID, FakeBridge, async_add_esphome_entry
from .common import async_create_target, async_setup_bridge


@pytest.fixture
async def loaded(hass: HomeAssistant):
    """A loaded bridge holding two commands, with nothing organised yet."""
    bridge = FakeBridge(hass)
    bridge.commands = {2: "probe_2", 4: "light"}
    esphome_entry = async_add_esphome_entry(hass)
    entry = await async_setup_bridge(hass, esphome_entry.entry_id)
    bridge.calls.clear()
    return bridge, entry


async def test_creating_a_target_makes_exactly_one_device(hass, loaded) -> None:
    """One piece of equipment, one Home Assistant device - hung off the bridge."""
    bridge, entry = loaded
    target = await async_create_target(hass, entry, "Bedroom Fan", "fan")

    registry = dr.async_get(hass)
    identifier = (DOMAIN, f"{BRIDGE_ID}_target_{target.target_id}")
    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device is not None
    assert device.name == "Bedroom Fan"
    assert device.model == "RF fan"
    assert device.config_subentry_id == target.subentry_id

    # Exactly one, and it is a child of the bridge rather than a sibling of it.
    ours = [
        item
        for item in dr.async_entries_for_config_entry(registry, entry.entry_id)
        if item.name == "Bedroom Fan"
    ]
    assert len(ours) == 1
    bridge_device = registry.async_get_device_by_identifier(
        (DOMAIN, BRIDGE_ID), entry.entry_id
    )
    assert device.via_device_id == bridge_device.id

    assert bridge.mutations == [], "creating a target wrote to the bridge"


async def test_target_id_survives_every_edit(hass, loaded) -> None:
    """Name, type and area are all mutable. Identity is not."""
    bridge, entry = loaded
    target = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    registry = dr.async_get(hass)
    identifier = (DOMAIN, f"{BRIDGE_ID}_target_{target.target_id}")
    device_id = registry.async_get_device_by_identifier(
        identifier, entry.entry_id
    ).id

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TARGET),
        context={"source": "reconfigure", "subentry_id": target.subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "Living Room Fan", CONF_TARGET_TYPE: "cover"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    renamed = targets(entry)[target.target_id]
    assert renamed.name == "Living Room Fan"
    assert renamed.target_type == "cover"
    assert renamed.target_id == target.target_id
    assert renamed.subentry_id == target.subentry_id

    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device.id == device_id, "the device was replaced rather than updated"
    assert device.name == "Living Room Fan"
    assert device.model == "RF cover"
    assert bridge.mutations == []


async def test_a_chosen_area_is_applied_once_and_never_reapplied(hass, loaded) -> None:
    """The area is a starting point. Where the user puts the device afterwards is theirs."""
    bridge, entry = loaded
    areas = ar.async_get(hass)
    bedroom = areas.async_get_or_create("Bedroom")
    kitchen = areas.async_get_or_create("Kitchen")

    target = await async_create_target(hass, entry, "Bedroom Fan", "fan", bedroom.id)
    registry = dr.async_get(hass)
    identifier = (DOMAIN, f"{BRIDGE_ID}_target_{target.target_id}")
    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device.area_id == bedroom.id

    # The user moves it, then Home Assistant restarts.
    registry.async_update_device(device.id, area_id=kitchen.id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device.area_id == kitchen.id, "the stored area overwrote the user's move"


async def test_changing_the_area_in_the_form_does_move_the_device(hass, loaded) -> None:
    """The third area rule: an explicit change in the form is an instruction, so it is obeyed.

    Together with the test above this pins the whole semantic. Creating a target seeds the area;
    a reload never touches it; and a user who opens the form and picks a different area gets the
    move they asked for. A form offering a field that silently did nothing would be the bug.
    """
    bridge, entry = loaded
    areas = ar.async_get(hass)
    bedroom = areas.async_get_or_create("Bedroom")
    hallway = areas.async_get_or_create("Hallway")

    target = await async_create_target(hass, entry, "Garage Gate", "gate", bedroom.id)
    registry = dr.async_get(hass)
    identifier = (DOMAIN, f"{BRIDGE_ID}_target_{target.target_id}")
    assert registry.async_get_device_by_identifier(
        identifier, entry.entry_id
    ).area_id == bedroom.id

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TARGET),
        context={"source": "reconfigure", "subentry_id": target.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Garage Gate", CONF_TARGET_TYPE: "gate", CONF_AREA_ID: hallway.id},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device.area_id == hallway.id, "the explicit area change was ignored"
    # And it sticks: a reload must not put it back where it started.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        registry.async_get_device_by_identifier(identifier, entry.entry_id).area_id
        == hallway.id
    )
    assert bridge.mutations == [], "changing an area reached the bridge"


async def test_the_form_offers_where_the_device_actually_is(hass, loaded) -> None:
    """The form must pre-fill the device's real area, not the one stored at creation.

    Those two diverge the moment the user moves the device themselves. If the form kept offering
    the stored value, it would show one area while the device sat in another, and re-selecting
    the area shown would look like no change at all and move nothing - which is precisely the bug
    the live run caught.
    """
    _bridge, entry = loaded
    areas = ar.async_get(hass)
    bedroom = areas.async_get_or_create("Bedroom")
    kitchen = areas.async_get_or_create("Kitchen")

    target = await async_create_target(hass, entry, "Bedroom Fan", "fan", bedroom.id)
    registry = dr.async_get(hass)
    identifier = (DOMAIN, f"{BRIDGE_ID}_target_{target.target_id}")
    device_id = registry.async_get_device_by_identifier(identifier, entry.entry_id).id

    # The user moves the device itself.
    registry.async_update_device(device_id, area_id=kitchen.id)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TARGET),
        context={"source": "reconfigure", "subentry_id": target.subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    offered = {
        str(key): key.default() for key in result["data_schema"].schema if key.default
    }
    assert offered.get(CONF_AREA_ID) == kitchen.id, (
        "the form offered the stored area rather than where the device is"
    )

    # Selecting the area it offered is not a request to move anything.
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Living Room Fan", CONF_TARGET_TYPE: "fan", CONF_AREA_ID: kitchen.id},
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"

    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device.name == "Living Room Fan"
    assert device.area_id == kitchen.id, "a rename dragged the device out of the user's area"


async def test_putting_a_manually_moved_device_back_is_obeyed(hass, loaded) -> None:
    """The live scenario: move the device by hand, then use the form to put it back.

    The stored area and the chosen area are equal here, so a comparison against the stored value
    sees no change and does nothing. Only comparing against the device's real area gets this
    right, and getting it wrong leaves the user pressing Submit with no effect.
    """
    _bridge, entry = loaded
    areas = ar.async_get(hass)
    bedroom = areas.async_get_or_create("Bedroom")
    kitchen = areas.async_get_or_create("Kitchen")

    target = await async_create_target(hass, entry, "Bedroom Fan", "fan", bedroom.id)
    registry = dr.async_get(hass)
    identifier = (DOMAIN, f"{BRIDGE_ID}_target_{target.target_id}")
    device_id = registry.async_get_device_by_identifier(identifier, entry.entry_id).id
    registry.async_update_device(device_id, area_id=kitchen.id)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TARGET),
        context={"source": "reconfigure", "subentry_id": target.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_NAME: "Bedroom Fan", CONF_TARGET_TYPE: "fan", CONF_AREA_ID: bedroom.id},
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"

    device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    assert device.area_id == bedroom.id, "the explicit move back was ignored"

    # And it survives a reload rather than snapping back to wherever it was.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        registry.async_get_device_by_identifier(identifier, entry.entry_id).area_id
        == bedroom.id
    )


async def test_a_duplicate_target_name_is_refused(hass, loaded) -> None:
    """Two devices called the same thing would be indistinguishable in every picker."""
    _bridge, entry = loaded
    await async_create_target(hass, entry, "Bedroom Fan", "fan")

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TARGET), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "bedroom fan", CONF_TARGET_TYPE: "fan"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "target_name_taken"}
    assert len(targets(entry)) == 1


async def test_targets_survive_a_reload_without_duplicating(hass, loaded) -> None:
    """A reload re-registers the same devices, it does not accumulate new ones."""
    bridge, entry = loaded
    first = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    second = await async_create_target(hass, entry, "Garage Gate", "gate")

    registry = dr.async_get(hass)
    before = {
        device.id
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
    }

    for _ in range(2):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    after = {
        device.id
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before, "a reload created or dropped devices"
    assert set(targets(entry)) == {first.target_id, second.target_id}
    assert bridge.mutations == []
