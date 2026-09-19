"""Learning a command straight onto a target.

The flow has to place the command itself rather than let the poller find it first, because a
command that appears unassigned and is re-parented a moment later is a command that flickers
between two devices in front of the user. The reservation in the reconciler exists for that, and
the first test here is what holds it honest.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.rf_cloner.const import (
    CONF_ICON,
    CONF_NAME,
    CONF_TARGET_ID,
    DOMAIN,
)
from custom_components.rf_cloner.targets import assigned_target, command_meta

from .bridge import BRIDGE_ID, FakeBridge, async_add_esphome_entry
from .common import async_create_target, async_setup_bridge


@pytest.fixture
async def loaded(hass: HomeAssistant):
    """A loaded bridge with nothing learned yet."""
    bridge = FakeBridge(hass)
    esphome_entry = async_add_esphome_entry(hass)
    entry = await async_setup_bridge(hass, esphome_entry.entry_id)
    bridge.calls.clear()
    return bridge, entry


async def _learn(hass: HomeAssistant, entry, user_input: dict) -> dict:
    """Walk the options flow's learn path to the end."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_command"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    # The capture runs as a progress step; let it finish and follow it through.
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    return result


async def test_learning_into_a_target_lands_there_directly(hass, loaded) -> None:
    """One command, one place, from the moment it exists."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")

    result = await _learn(
        hass,
        entry,
        {CONF_NAME: "speed_1", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:fan-speed-1"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "learned", result

    command_id = next(
        cid for cid, name in bridge.commands.items() if name == "speed_1"
    )
    assert assigned_target(entry, command_id).target_id == fan.target_id
    assert command_meta(entry)[command_id].icon == "mdi:fan-speed-1"

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "button", DOMAIN, f"{BRIDGE_ID}_{command_id}"
    )
    assert entity_id is not None
    record = registry.async_get(entity_id)
    assert record.config_subentry_id == fan.subentry_id
    assert record.device_id is not None
    assert hass.states.get(entity_id).attributes["icon"] == "mdi:fan-speed-1"


async def test_learning_globally_can_stay_unassigned(hass, loaded) -> None:
    """Grouping is optional, and choosing not to group is a first-class answer."""
    bridge, entry = loaded

    result = await _learn(
        hass, entry, {CONF_NAME: "probe_9", CONF_TARGET_ID: "__unassigned__"}
    )
    assert result["reason"] == "learned"

    command_id = next(
        cid for cid, name in bridge.commands.items() if name == "probe_9"
    )
    assert assigned_target(entry, command_id) is None
    record = er.async_get(hass).async_get(
        er.async_get(hass).async_get_entity_id(
            "button", DOMAIN, f"{BRIDGE_ID}_{command_id}"
        )
    )
    assert record.config_subentry_id is None
    assert record.device_id is None


async def test_an_unnamed_icon_is_suggested_from_the_name_and_type(hass, loaded) -> None:
    """The icon field may be left empty; something sensible still arrives."""
    bridge, entry = loaded
    gate = await async_create_target(hass, entry, "Garage Gate", "gate")

    await _learn(hass, entry, {CONF_NAME: "open", CONF_TARGET_ID: gate.target_id})

    command_id = next(cid for cid, name in bridge.commands.items() if name == "open")
    assert command_meta(entry)[command_id].icon == "mdi:gate-open"


async def test_a_failed_capture_leaves_nothing_behind(hass, loaded) -> None:
    """A learn that captured nothing must not leave a half-organised command."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Bedroom Fan", "fan")
    bridge.capture_succeeds = False

    result = await _learn(
        hass, entry, {CONF_NAME: "speed_1", CONF_TARGET_ID: fan.target_id}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "learn_failed"

    assert bridge.commands == {}
    assert command_meta(entry) == {}
    assert (
        er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{BRIDGE_ID}_5")
        is None
    )


async def test_a_duplicate_command_name_is_refused(hass, loaded) -> None:
    """The bridge keys its own web UI by name, so two commands cannot share one."""
    bridge, entry = loaded
    bridge.commands = {4: "light"}
    # The form checks the last status the integration read, so let it read this one.
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    bridge.calls.clear()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_command"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_NAME: "light", CONF_TARGET_ID: "__unassigned__"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "name_taken"}
    assert bridge.mutations == []
