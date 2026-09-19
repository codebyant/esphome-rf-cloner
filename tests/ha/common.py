"""Shared setup for the target, assignment and icon tests."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rf_cloner.const import (
    CONF_ACTION_PREFIX,
    CONF_AREA_ID,
    CONF_ESPHOME_ENTRY_ID,
    CONF_NAME,
    CONF_TARGET_TYPE,
    DOMAIN,
    ENTRY_MINOR_VERSION,
    ENTRY_VERSION,
    SUBENTRY_TYPE_TARGET,
)
from custom_components.rf_cloner.targets import RfTarget, targets

from .bridge import BRIDGE_ID


async def async_setup_bridge(
    hass: HomeAssistant, esphome_entry_id: str
) -> MockConfigEntry:
    """A loaded 0.2 bridge entry, with no targets and no organisation yet."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        title="RF Bridge",
        version=ENTRY_VERSION,
        minor_version=ENTRY_MINOR_VERSION,
        data={CONF_ESPHOME_ENTRY_ID: esphome_entry_id, CONF_ACTION_PREFIX: "rf_"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def async_create_target(
    hass: HomeAssistant,
    entry: ConfigEntry,
    name: str,
    target_type: str = "fan",
    area_id: str | None = None,
) -> RfTarget:
    """Create a target through its real subentry flow, as the UI would."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_TARGET), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM, result

    user_input: dict[str, Any] = {CONF_NAME: name, CONF_TARGET_TYPE: target_type}
    if area_id is not None:
        user_input[CONF_AREA_ID] = area_id
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY, result

    found = [target for target in targets(entry).values() if target.name == name]
    assert len(found) == 1, f"expected exactly one target called {name}"
    return found[0]


async def async_options_step(
    hass: HomeAssistant, entry: ConfigEntry, step: str, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Walk the options flow menu to `step` and submit `user_input` to it."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU, result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["type"] is FlowResultType.FORM, result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    return result


async def async_edit_command(
    hass: HomeAssistant,
    entry: ConfigEntry,
    command_id: int,
    user_input: dict[str, Any],
) -> dict[str, Any]:
    """Pick one command in the options flow and submit the edit form for it."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "manage_command"}
    )
    assert result["type"] is FlowResultType.FORM, result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"command_id": str(command_id)}
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "command", result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    return result
