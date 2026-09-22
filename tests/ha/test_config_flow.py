"""The initial config flow, from the shape of the form to the entry it creates.

The v0.2 suite built its bridge entries with `MockConfigEntry` and only ever exercised the
subentry, options and reconfigure flows, so `async_step_user` was never run and the form it shows
was never looked at. These tests run it, and - crucially - assert on the *serialized* form, the
JSON the frontend actually receives, because a schema that is correct in Python can still reach
the browser as something it cannot render.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.rf_cloner.const import (
    CONF_ACTION_PREFIX,
    CONF_ESPHOME_ENTRY_ID,
    DEFAULT_ACTION_PREFIX,
    DOMAIN,
)

from .bridge import BRIDGE_ID, FakeBridge, async_add_esphome_entry

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _serialized_user_step(hass: HomeAssistant, hass_client) -> dict[str, Any]:
    """Start the flow over HTTP, the way the frontend does, and return its JSON.

    Going through the view rather than `flow.async_init` is the whole point: it runs Home
    Assistant's schema serializer, which is where a form can lose its fields on the way out.
    """
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/flow",
        json={"handler": DOMAIN, "show_advanced_options": True},
    )
    assert response.status == 200, await response.text()
    return await response.json()


def _field(schema: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """The serialized field called `name`, or a readable failure naming what was there."""
    found = [field for field in schema if field.get("name") == name]
    assert found, f"no field named {name} in {[f.get('name') for f in schema]}"
    return found[0]


# The form itself


async def test_user_step_form_is_not_empty(hass: HomeAssistant, hass_client) -> None:
    """The first thing a user sees has fields in it."""
    async_add_esphome_entry(hass)
    FakeBridge(hass)

    result = await _serialized_user_step(hass, hass_client)

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] in ({}, None)
    schema = result["data_schema"]
    assert isinstance(schema, list), f"data_schema serialized as {type(schema).__name__}"
    assert schema, "the user step serialized to an empty form"


async def test_user_step_offers_an_esphome_entry_picker(
    hass: HomeAssistant, hass_client
) -> None:
    """The ESPHome node is chosen from a picker the frontend can render."""
    async_add_esphome_entry(hass)
    FakeBridge(hass)

    result = await _serialized_user_step(hass, hass_client)
    field = _field(result["data_schema"], CONF_ESPHOME_ENTRY_ID)

    assert field["required"] is True
    selector = field["selector"]
    # Either selector expresses the same relationship; what matters is that exactly one is
    # offered, and that it is one the frontend knows how to draw.
    assert len(selector) == 1, selector
    assert next(iter(selector)) in ("config_entry", "select"), selector


async def test_user_step_offers_the_action_prefix(
    hass: HomeAssistant, hass_client
) -> None:
    """The prefix is optional and pre-filled with the default."""
    async_add_esphome_entry(hass)
    FakeBridge(hass)

    result = await _serialized_user_step(hass, hass_client)
    field = _field(result["data_schema"], CONF_ACTION_PREFIX)

    assert DEFAULT_ACTION_PREFIX == "rf_"
    assert field["default"] == DEFAULT_ACTION_PREFIX
    assert field["required"] is False
    assert "text" in field["selector"]


async def test_user_step_strings_cover_every_field(
    hass: HomeAssistant, hass_client
) -> None:
    """Every field the form serializes has a label, so none of them renders nameless."""
    async_add_esphome_entry(hass)
    FakeBridge(hass)

    result = await _serialized_user_step(hass, hass_client)
    strings = json.loads(
        (REPO_ROOT / "custom_components/rf_cloner/strings.json").read_text(
            encoding="utf-8"
        )
    )
    step = strings["config"]["step"]["user"]

    assert step["description"]
    for field in result["data_schema"]:
        assert step["data"][field["name"]], field["name"]


# Submitting it


async def test_submitting_creates_the_entry(hass: HomeAssistant) -> None:
    """A good node produces an entry bound to it, identified by the bridge it reported."""
    esphome = async_add_esphome_entry(hass)
    FakeBridge(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ESPHOME_ENTRY_ID: esphome.entry_id}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_ESPHOME_ENTRY_ID] == esphome.entry_id
    assert result["data"][CONF_ACTION_PREFIX] == DEFAULT_ACTION_PREFIX

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.data[CONF_ESPHOME_ENTRY_ID] == esphome.entry_id
    assert entry.unique_id == BRIDGE_ID
    assert entry.title == "RF Bridge"


async def test_duplicate_bridge_aborts(hass: HomeAssistant) -> None:
    """The same logical bridge cannot be configured twice, even through another node."""
    esphome = async_add_esphome_entry(hass)
    FakeBridge(hass)
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        data={CONF_ESPHOME_ENTRY_ID: esphome.entry_id, CONF_ACTION_PREFIX: "rf_"},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ESPHOME_ENTRY_ID: esphome.entry_id}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# The ways it can go wrong


async def test_node_without_rf_cloner_actions_is_reported(hass: HomeAssistant) -> None:
    """An ESPHome node that is not a bridge says so, and keeps the form on screen."""
    esphome = async_add_esphome_entry(hass)  # no FakeBridge: no rf_ actions registered

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ESPHOME_ENTRY_ID: esphome.entry_id}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_bridge_actions"}
    assert result["data_schema"] is not None


async def test_unavailable_bridge_is_reported(hass: HomeAssistant) -> None:
    """A node whose status action fails is a connection problem, not a missing bridge."""
    esphome = async_add_esphome_entry(hass)
    bridge = FakeBridge(hass)
    status_service = bridge._service("status")

    def _explode(call):
        raise RuntimeError("no answer")

    hass.services.async_remove("esphome", status_service)
    hass.services.async_register("esphome", status_service, _explode)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ESPHOME_ENTRY_ID: esphome.entry_id}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_missing_esphome_entry_is_reported(hass: HomeAssistant) -> None:
    """A node removed while the form was open names its own failure, not `unknown`.

    The picker only ever offers entries that existed when the form was drawn, so the way to
    reach this is to delete one in between - which is the race the error exists for.
    """
    # Removing the entry unloads its integration, and the real ESPHome one cannot be imported
    # without aioesphomeapi; the entry is all this test needs from it.
    mock_integration(hass, MockModule("esphome"))
    esphome = async_add_esphome_entry(hass)
    survivor = MockConfigEntry(
        domain="esphome",
        unique_id="02:00:00:00:00:02",
        data={"device_name": "other-node", "host": "127.0.0.2"},
        title="Other node",
    )
    survivor.add_to_hass(hass)
    FakeBridge(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM

    # One node goes away while the form is open; another remains, so the step still has
    # something to offer and reaches the probe rather than the empty-list abort.
    await hass.config_entries.async_remove(esphome.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ESPHOME_ENTRY_ID: esphome.entry_id}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "esphome_entry_missing"}


async def test_an_unoffered_entry_id_is_rejected(hass: HomeAssistant) -> None:
    """The picker is the only way in: an id that was never offered does not reach the probe."""
    async_add_esphome_entry(hass)
    FakeBridge(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ESPHOME_ENTRY_ID: "01JZZZZZZZZZZZZZZZZZZZZZZZ"}
        )
    assert not hass.config_entries.async_entries(DOMAIN)


# How many ESPHome nodes there are


async def test_multiple_esphome_entries_can_be_told_apart(
    hass: HomeAssistant, hass_client
) -> None:
    """With two nodes the form still offers a choice, and the chosen one is the one bound."""
    first = async_add_esphome_entry(hass)
    second = MockConfigEntry(
        domain="esphome",
        unique_id="02:00:00:00:00:02",
        data={"device_name": "other-node", "host": "127.0.0.2"},
        title="Other node",
    )
    second.add_to_hass(hass)
    FakeBridge(hass)

    result = await _serialized_user_step(hass, hass_client)
    selector = _field(result["data_schema"], CONF_ESPHOME_ENTRY_ID)["selector"]
    if "select" in selector:
        options = selector["select"]["options"]
        assert {option["value"] for option in options} == {
            first.entry_id,
            second.entry_id,
        }
        assert {option["label"] for option in options} == {"RF Bridge", "Other node"}

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ESPHOME_ENTRY_ID: first.entry_id}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ESPHOME_ENTRY_ID] == first.entry_id


async def test_single_esphome_entry_still_offers_a_choice(
    hass: HomeAssistant, hass_client
) -> None:
    """One node is offered, not silently adopted: the user still confirms which it is."""
    esphome = async_add_esphome_entry(hass)
    FakeBridge(hass)

    result = await _serialized_user_step(hass, hass_client)

    assert result["type"] == "form"
    selector = _field(result["data_schema"], CONF_ESPHOME_ENTRY_ID)["selector"]
    if "select" in selector:
        assert [option["value"] for option in selector["select"]["options"]] == [
            esphome.entry_id
        ]
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_no_esphome_entries_is_not_a_blank_form(
    hass: HomeAssistant, hass_client
) -> None:
    """With nothing to pick, the flow explains itself instead of showing an unusable form."""
    result = await _serialized_user_step(hass, hass_client)

    assert result["type"] == "abort", result
    assert result["reason"] == "no_esphome_entries"


@pytest.mark.parametrize("prefix", ["rf_", "custom_"])
async def test_action_prefix_is_honoured(hass: HomeAssistant, prefix: str) -> None:
    """A non-default prefix reaches the transport and is stored on the entry."""
    esphome = async_add_esphome_entry(hass)
    bridge = FakeBridge(hass)
    if prefix != "rf_":
        for action in ("status", "export", "learn", "cancel", "rename", "delete_id"):
            original = bridge._service(action)
            hass.services.async_register(
                "esphome",
                original.replace("_rf_", f"_{prefix}"),
                hass.services.async_services()["esphome"][original].job.target,
                supports_response=hass.services.async_services()["esphome"][
                    original
                ].supports_response,
            )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ESPHOME_ENTRY_ID: esphome.entry_id, CONF_ACTION_PREFIX: prefix},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"][CONF_ACTION_PREFIX] == prefix


# Re-pointing an existing bridge, which shares the same picker


async def test_reconfigure_form_is_not_empty(hass: HomeAssistant, hass_client) -> None:
    """The reconfigure step renders too, and starts on the node the bridge is bound to."""
    esphome = async_add_esphome_entry(hass)
    FakeBridge(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        title="RF Bridge",
        data={CONF_ESPHOME_ENTRY_ID: esphome.entry_id, CONF_ACTION_PREFIX: "rf_"},
    )
    entry.add_to_hass(hass)

    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/flow",
        json={
            "handler": DOMAIN,
            "show_advanced_options": True,
            "entry_id": entry.entry_id,
        },
    )
    assert response.status == 200, await response.text()
    result = await response.json()

    assert result["step_id"] == "reconfigure"
    schema = result["data_schema"]
    assert isinstance(schema, list) and schema, schema
    field = _field(schema, CONF_ESPHOME_ENTRY_ID)
    assert field["default"] == esphome.entry_id
    assert next(iter(field["selector"])) in ("config_entry", "select")


async def test_reconfigure_drops_a_default_that_no_longer_exists(
    hass: HomeAssistant, hass_client
) -> None:
    """If the bound node is gone, the form does not start on a choice it cannot offer."""
    mock_integration(hass, MockModule("esphome"))
    gone = async_add_esphome_entry(hass)
    replacement = MockConfigEntry(
        domain="esphome",
        unique_id="02:00:00:00:00:03",
        data={"device_name": "new-node", "host": "127.0.0.3"},
        title="New node",
    )
    replacement.add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        title="RF Bridge",
        data={CONF_ESPHOME_ENTRY_ID: gone.entry_id, CONF_ACTION_PREFIX: "rf_"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_remove(gone.entry_id)
    await hass.async_block_till_done()

    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    response = await client.post(
        "/api/config/config_entries/flow",
        json={
            "handler": DOMAIN,
            "show_advanced_options": True,
            "entry_id": entry.entry_id,
        },
    )
    result = await response.json()

    assert result["step_id"] == "reconfigure"
    field = _field(result["data_schema"], CONF_ESPHOME_ENTRY_ID)
    assert "default" not in field, field
    if "select" in field["selector"]:
        assert [option["value"] for option in field["selector"]["select"]["options"]] == [
            replacement.entry_id
        ]
