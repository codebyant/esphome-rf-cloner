"""The `Learn command` action on the integration page.

In 0.2.0 the only way to learn a command was Configure -> RF commands -> Learn a command: two
screens behind the bridge's gear, for the thing this integration exists to do. Home Assistant
renders one button per key of `async_get_supported_subentry_types` and offers nothing else that
puts a named action at the top of an integration page, so learning is registered as a second key.

That makes these tests about two claims, and both need proving separately. The first is that Home
Assistant is *told* about the action in the shape its frontend reads - a button that never renders
is worth nothing, and the shape is not something the flow itself can vouch for. The second is that
the action reaches the same learn as the menu does, rather than a second implementation of it that
happens to agree today.

The flow deliberately creates no subentry. `tests_no_subentry_is_created` is the guard on that:
a command's entity has to live on its target's device, an entity belongs to exactly one subentry,
so a `learn_command` subentry would be a thing no command could ever be attached to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component
import pytest

from custom_components.rf_cloner.const import (
    ACTION_TYPE_LEARN_COMMAND,
    CONF_ICON,
    CONF_NAME,
    CONF_TARGET_ID,
    DOMAIN,
    SUBENTRY_TYPE_TARGET,
)
from custom_components.rf_cloner.targets import assigned_target, command_meta, targets

from .bridge import BRIDGE_ID, FakeBridge, async_add_esphome_entry
from .common import async_create_target, async_setup_bridge

REPO_ROOT = Path(__file__).resolve().parents[2]

UNASSIGNED = "__unassigned__"


@pytest.fixture
async def loaded(hass: HomeAssistant):
    """A loaded bridge with nothing learned yet."""
    bridge = FakeBridge(hass)
    esphome_entry = async_add_esphome_entry(hass)
    entry = await async_setup_bridge(hass, esphome_entry.entry_id)
    bridge.calls.clear()
    return bridge, entry


async def _start(hass: HomeAssistant, entry) -> dict[str, Any]:
    """Press the action, the way the integration page's button does."""
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, ACTION_TYPE_LEARN_COMMAND), context={"source": "user"}
    )


async def _learn_via_action(
    hass: HomeAssistant, entry, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Press the action and follow the learn through to whatever ends it."""
    result = await _start(hass, entry)
    assert result["type"] is FlowResultType.FORM, result
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input
    )
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.subentries.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    return result


async def _learn_via_menu(
    hass: HomeAssistant, entry, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Learn the old way, through Configure -> RF commands -> Learn a command."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU, result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "learn_command"}
    )
    assert result["type"] is FlowResultType.FORM, result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    await hass.async_block_till_done()
    return result


def _command_entities(hass: HomeAssistant) -> list[str]:
    """Every button this bridge owns that stands for a learned command.

    The bridge's own `cancel_learning` button is a button of this platform too, so counting the
    platform's buttons would count it. Command entities are the ones keyed by a command id.
    """
    registry = er.async_get(hass)
    return sorted(
        entity.entity_id
        for entity in registry.entities.values()
        if entity.platform == DOMAIN
        and entity.domain == "button"
        and entity.unique_id.startswith(f"{BRIDGE_ID}_")
        and entity.unique_id.rsplit("_", 1)[-1].isdigit()
    )


def _field(schema: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """The serialized field called `name`, or a readable failure naming what was there."""
    found = [field for field in schema if field.get("name") == name]
    assert found, f"no field named {name} in {[f.get('name') for f in schema]}"
    return found[0]


def _options(field: dict[str, Any]) -> list[dict[str, str]]:
    """The select options of a serialized picker field."""
    assert "select" in field["selector"], field["selector"]
    return field["selector"]["select"]["options"]


async def _serialized_form(hass: HomeAssistant, hass_client, entry) -> dict[str, Any]:
    """Start the action over HTTP and return the JSON the frontend would render.

    Going through the view rather than the flow manager is the point: it runs Home Assistant's
    schema serializer, which is where a form that is correct in Python can still reach the browser
    as something it cannot draw. That is exactly how the 0.2.0 config flow shipped empty.
    """
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/subentries/flow",
        json={"handler": [entry.entry_id, ACTION_TYPE_LEARN_COMMAND]},
    )
    assert response.status == 200, await response.text()
    return await response.json()


# What Home Assistant is told the action is


async def test_the_action_is_announced_to_the_frontend(hass, loaded) -> None:
    """The button exists because the entry says the action does, in the field the page reads."""
    _bridge, entry = loaded

    supported = entry.supported_subentry_types

    assert ACTION_TYPE_LEARN_COMMAND in supported, supported
    # The page renders `Add an RF device` from the same mapping, and the point of this change is
    # that the two now sit side by side.
    assert SUBENTRY_TYPE_TARGET in supported, supported


async def test_the_action_reaches_the_frontend_over_the_api(
    hass: HomeAssistant, loaded, hass_ws_client
) -> None:
    """`supported_subentry_types` is what the integration page iterates, so assert on the wire."""
    _bridge, entry = loaded
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "config_entries/get", "domain": DOMAIN})
    response = await client.receive_json()

    assert response["success"], response
    rows = [row for row in response["result"] if row["entry_id"] == entry.entry_id]
    assert len(rows) == 1, response["result"]
    assert ACTION_TYPE_LEARN_COMMAND in rows[0]["supported_subentry_types"]


async def test_the_button_is_labelled_learn_command(hass, loaded) -> None:
    """The frontend has no fallback for this label: no string, no text on the button."""
    strings = json.loads(
        (REPO_ROOT / "custom_components/rf_cloner/strings.json").read_text(
            encoding="utf-8"
        )
    )
    action = strings["config_subentries"][ACTION_TYPE_LEARN_COMMAND]

    assert action["initiate_flow"]["user"] == "Learn command"
    # Required by hassfest for every key of `config_subentries`, whether or not one is ever made.
    assert action["entry_type"]


async def test_the_forms_strings_are_all_present(hass, loaded, hass_client) -> None:
    """Every field the action shows has a label under this action's own translation key.

    The subentry dialog looks these up at `config_subentries.<type>.step.<step>.data.<field>` and
    falls back to the raw field name, so a missing string shows the user `target_id`.
    """
    _bridge, entry = loaded
    result = await _serialized_form(hass, hass_client, entry)
    strings = json.loads(
        (REPO_ROOT / "custom_components/rf_cloner/strings.json").read_text(
            encoding="utf-8"
        )
    )
    step = strings["config_subentries"][ACTION_TYPE_LEARN_COMMAND]["step"]["learn_command"]

    assert step["description"]
    for field in result["data_schema"]:
        assert step["data"][field["name"]], field["name"]


@pytest.mark.parametrize("language", ["en", "pt"])
async def test_every_string_the_dialog_asks_for_resolves(hass, loaded, language) -> None:
    """Resolve the action's translations the way the frontend does, key by key.

    The subentry dialog has no fallback worth having: a missing label leaves the button on the
    integration page with *no text at all*, a missing field label shows the user `target_id`, and
    a missing abort string shows the raw reason. None of that is visible from the flow's own
    result, so it has to be asserted against the translation machinery rather than the flow.

    A non-English language is included because only `translations/<language>.json` is read at
    runtime - `strings.json` is for hassfest - and this integration ships English alone, so the
    fallback to it is load-bearing for every other language.
    """
    base = f"component.{DOMAIN}.config_subentries.{ACTION_TYPE_LEARN_COMMAND}"
    expected = [
        f"{base}.initiate_flow.user",
        f"{base}.step.learn_command.title",
        f"{base}.step.learn_command.description",
        f"{base}.step.learn.title",
        f"{base}.progress.learning",
        *(
            f"{base}.step.learn_command.data.{field}"
            for field in (CONF_NAME, CONF_TARGET_ID, CONF_ICON)
        ),
        *(
            f"{base}.step.learn_command.data_description.{field}"
            for field in (CONF_NAME, CONF_TARGET_ID, CONF_ICON)
        ),
        *(
            f"{base}.abort.{reason}"
            for reason in (
                "learned",
                "learn_failed",
                "not_loaded",
                "not_writable",
                "registry_full",
            )
        ),
        *(
            f"{base}.error.{key}"
            for key in ("name_empty", "name_too_long", "name_invalid", "name_taken")
        ),
    ]

    resources = await async_get_translations(
        hass, language, "config_subentries", {DOMAIN}
    )

    assert [key for key in expected if not resources.get(key)] == []


# Starting it


# Starting it


async def test_the_action_opens_the_learn_form_directly(hass, loaded) -> None:
    """One click, no menu. This is the whole reason the button exists."""
    _bridge, entry = loaded

    result = await _start(hass, entry)

    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "learn_command", result
    assert result["errors"] in ({}, None)


async def test_the_form_has_a_name_a_device_and_an_icon(
    hass, loaded, hass_client
) -> None:
    """The form the frontend receives is the one described in the briefing, and it is not empty."""
    _bridge, entry = loaded

    result = await _serialized_form(hass, hass_client, entry)

    assert result["type"] == "form", result
    assert result["step_id"] == "learn_command", result
    schema = result["data_schema"]
    assert isinstance(schema, list), f"data_schema serialized as {type(schema).__name__}"
    assert schema, "the learn form serialized to an empty form"
    assert _field(schema, CONF_NAME)["required"] is True
    assert "select" in _field(schema, CONF_TARGET_ID)["selector"]
    assert "icon" in _field(schema, CONF_ICON)["selector"]


async def test_unassigned_is_offered_with_no_targets_at_all(
    hass, loaded, hass_client
) -> None:
    """Learning must not require organising first; a bridge with no targets still learns."""
    _bridge, entry = loaded
    assert targets(entry) == {}

    result = await _serialized_form(hass, hass_client, entry)
    field = _field(result["data_schema"], CONF_TARGET_ID)

    assert [option["value"] for option in _options(field)] == [UNASSIGNED]
    assert field["default"] == UNASSIGNED


async def test_existing_targets_are_offered_by_id(hass, loaded, hass_client) -> None:
    """Targets are listed by name and chosen by id, so renaming one cannot break the form."""
    _bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")
    gate = await async_create_target(hass, entry, "Portao", "gate")

    result = await _serialized_form(hass, hass_client, entry)
    options = _options(_field(result["data_schema"], CONF_TARGET_ID))

    assert [option["value"] for option in options] == [
        UNASSIGNED,
        gate.target_id,
        fan.target_id,
    ], "targets should be listed after Unassigned, in human order"
    assert [option["label"] for option in options] == [
        "Unassigned",
        "Portao",
        "Ventilador",
    ]


async def test_a_renamed_target_keeps_its_id_in_the_form(
    hass, loaded, hass_client
) -> None:
    """The value the form submits is the target's identity, not the label beside it."""
    _bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")

    subentry = entry.subentries[fan.subentry_id]
    hass.config_entries.async_update_subentry(entry, subentry, title="Ventilador do Quarto")
    await hass.async_block_till_done()

    result = await _serialized_form(hass, hass_client, entry)
    options = _options(_field(result["data_schema"], CONF_TARGET_ID))

    assert [option["value"] for option in options] == [UNASSIGNED, fan.target_id]
    assert options[1]["label"] == "Ventilador do Quarto"


# What it refuses


async def test_a_duplicate_name_is_refused(hass, loaded) -> None:
    """The bridge keys its own web UI by name, so the action rejects a clash like the menu does."""
    bridge, entry = loaded
    bridge.commands = {4: "light"}
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    bridge.calls.clear()

    result = await _start(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_NAME: "light", CONF_TARGET_ID: UNASSIGNED}
    )

    assert result["type"] is FlowResultType.FORM, result
    assert result["errors"] == {CONF_NAME: "name_taken"}
    assert bridge.mutations == []


async def test_a_full_registry_blocks_the_action(hass, loaded) -> None:
    """No free slot means no learn, and the action says so before asking for a name."""
    bridge, entry = loaded
    bridge.commands = {index: f"cmd_{index}" for index in range(16)}
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    bridge.calls.clear()

    result = await _start(hass, entry)

    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "registry_full", result
    assert bridge.mutations == []


async def test_a_read_only_bridge_blocks_the_action(hass, loaded) -> None:
    """A bridge that is not accepting changes refuses the action, not the submission."""
    bridge, entry = loaded
    bridge.read_only = True
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    bridge.calls.clear()

    result = await _start(hass, entry)

    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "not_writable", result
    assert bridge.mutations == []


# What it does


async def test_learning_into_a_target_lands_on_that_device(hass, loaded) -> None:
    """The command learned from the button is born inside the RF device that was chosen."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")

    result = await _learn_via_action(
        hass,
        entry,
        {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id, CONF_ICON: "mdi:lightbulb"},
    )

    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "learned", result

    command_id = next(cid for cid, name in bridge.commands.items() if name == "light")
    assert assigned_target(entry, command_id).target_id == fan.target_id
    assert command_meta(entry)[command_id].icon == "mdi:lightbulb"

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, f"{BRIDGE_ID}_{command_id}")
    assert entity_id is not None
    record = registry.async_get(entity_id)
    assert record.config_subentry_id == fan.subentry_id
    assert record.device_id is not None
    assert hass.states.get(entity_id).attributes["icon"] == "mdi:lightbulb"


async def test_learning_unassigned_creates_no_device(hass, loaded) -> None:
    """Choosing not to group is a first-class answer here too."""
    bridge, entry = loaded

    result = await _learn_via_action(
        hass, entry, {CONF_NAME: "probe_9", CONF_TARGET_ID: UNASSIGNED}
    )
    assert result["reason"] == "learned", result

    command_id = next(cid for cid, name in bridge.commands.items() if name == "probe_9")
    assert assigned_target(entry, command_id) is None

    registry = er.async_get(hass)
    record = registry.async_get(
        registry.async_get_entity_id("button", DOMAIN, f"{BRIDGE_ID}_{command_id}")
    )
    assert record.config_subentry_id is None
    assert record.device_id is None


async def test_no_subentry_is_created(hass, loaded) -> None:
    """The action borrows the subentry mechanism for its button and creates nothing with it.

    If this ever fails, a command has become a subentry - which it cannot be, because its entity
    has to live on its target's device and an entity belongs to exactly one subentry.
    """
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")
    before = set(entry.subentries)

    await _learn_via_action(hass, entry, {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id})

    assert set(entry.subentries) == before
    assert [
        subentry.subentry_type
        for subentry in entry.subentries.values()
        if subentry.subentry_type == ACTION_TYPE_LEARN_COMMAND
    ] == []


async def test_the_icon_is_still_suggested_when_left_empty(hass, loaded) -> None:
    """The action runs the same placement as the menu, suggestion included."""
    bridge, entry = loaded
    gate = await async_create_target(hass, entry, "Portao", "gate")

    await _learn_via_action(hass, entry, {CONF_NAME: "open", CONF_TARGET_ID: gate.target_id})

    command_id = next(cid for cid, name in bridge.commands.items() if name == "open")
    assert command_meta(entry)[command_id].icon == "mdi:gate-open"


async def test_a_failed_capture_leaves_nothing_behind(hass, loaded) -> None:
    """The reservation is released on the way out of the action, as it is out of the menu."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")
    bridge.capture_succeeds = False

    result = await _learn_via_action(
        hass, entry, {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id}
    )

    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "learn_failed", result
    assert bridge.commands == {}
    assert command_meta(entry) == {}
    assert not entry.runtime_data.reconciler._reserved, "the name reservation outlived the learn"


# The two entrances are one learn


async def test_the_old_menu_path_still_works(hass, loaded) -> None:
    """The gear route is kept: the button is an additional door, not a replacement one."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")

    result = await _learn_via_menu(
        hass, entry, {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id}
    )

    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "learned", result
    command_id = next(cid for cid, name in bridge.commands.items() if name == "light")
    assert assigned_target(entry, command_id).target_id == fan.target_id


async def test_the_action_creates_exactly_one_entity(hass, loaded) -> None:
    """One learn, one button. The action must not double anything the menu already does."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")

    await _learn_via_action(hass, entry, {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id})

    assert _command_entities(hass) == ["button.light"]


async def test_both_entrances_produce_the_same_record(hass, loaded) -> None:
    """Two doors, one learn. Ids stay monotonic and both commands land on the same device.

    The refresh between the two learns is not decoration. `async_handle_entry_update` prunes the
    entry's command metadata against `coordinator.data`, so until the bridge has been polled again
    the listing it prunes against predates the command the flow has just written - and the write
    is undone. That is a 0.2.0 defect on the options-flow path, not something this action
    introduced, and it is left alone here rather than fixed in a UX change; see the notes in the
    pull request. Real use hits it inside the 30-second poll interval, which is exactly the gap a
    one-click button makes easy to land in.
    """
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")

    await _learn_via_menu(hass, entry, {CONF_NAME: "via_menu", CONF_TARGET_ID: fan.target_id})
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    await _learn_via_action(hass, entry, {CONF_NAME: "via_button", CONF_TARGET_ID: fan.target_id})

    by_name = {name: cid for cid, name in bridge.commands.items()}
    assert set(by_name) == {"via_menu", "via_button"}
    assert by_name["via_button"] > by_name["via_menu"], "command ids must stay monotonic"

    meta = command_meta(entry)
    assert meta[by_name["via_menu"]] == meta[by_name["via_button"]], (
        "the same choices through either door must produce the same record"
    )

    registry = er.async_get(hass)
    records = [
        registry.async_get(
            registry.async_get_entity_id("button", DOMAIN, f"{BRIDGE_ID}_{cid}")
        )
        for cid in by_name.values()
    ]
    assert all(record is not None for record in records), _command_entities(hass)
    assert {record.config_subentry_id for record in records} == {fan.subentry_id}
    assert len({record.device_id for record in records}) == 1
    assert len(_command_entities(hass)) == 2, _command_entities(hass)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "0.2.0 defect, not introduced here: async_step_learned writes the new command's metadata,"
        " the entry update listener runs async_handle_entry_update, and that prunes against the"
        " last polled status - which does not list the command yet. The second learn inside one"
        " poll interval loses its target and icon. Reproduced through the options menu too, so it"
        " is not specific to the action. Delete this marker when reconcile.py stops pruning"
        " commands newer than the listing it is pruning against."
    ),
)
async def test_two_learns_inside_one_poll_interval_both_keep_their_target(
    hass, loaded
) -> None:
    """Learning twice without waiting for a poll must not silently unassign the second command."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")

    await _learn_via_action(hass, entry, {CONF_NAME: "one", CONF_TARGET_ID: fan.target_id})
    await _learn_via_action(hass, entry, {CONF_NAME: "two", CONF_TARGET_ID: fan.target_id})

    second = next(cid for cid, name in bridge.commands.items() if name == "two")
    assert assigned_target(entry, second) is not None, (
        "the second command lost the target the user chose for it"
    )
    assert assigned_target(entry, second).target_id == fan.target_id


async def test_the_entry_reloads_cleanly_after_the_action(hass, loaded) -> None:
    """A learn through the button leaves the entry in a state that survives a reload."""
    bridge, entry = loaded
    fan = await async_create_target(hass, entry, "Ventilador", "fan")
    await _learn_via_action(hass, entry, {CONF_NAME: "light", CONF_TARGET_ID: fan.target_id})
    command_id = next(cid for cid, name in bridge.commands.items() if name == "light")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("button", DOMAIN, f"{BRIDGE_ID}_{command_id}")
    assert entity_id is not None
    assert registry.async_get(entity_id).config_subentry_id == fan.subentry_id
    assert assigned_target(entry, command_id).target_id == fan.target_id
    ours = _command_entities(hass)
    assert len(ours) == 1, ours
