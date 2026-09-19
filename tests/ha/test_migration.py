"""Upgrading a real 0.1 installation.

0.1 is public, so this is the first upgrade that has to preserve somebody's existing setup. What
matters is not that the migration runs, but that everything a user could have built on top of
those entities still points at them afterwards: the same entity ids, the same unique ids, the
same customisations, and the same commands on the bridge.

The fixture below is the shape a 0.1 entry really has - it is the shape the development instance
was found in, with two command subentries whose entities carry no device.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rf_cloner.const import (
    CONF_ACTION_PREFIX,
    CONF_ESPHOME_ENTRY_ID,
    DOMAIN,
    SUBENTRY_TYPE_COMMAND,
)
from custom_components.rf_cloner.targets import assigned_target, command_meta

from .bridge import BRIDGE_ID, FakeBridge, async_add_esphome_entry

# The two commands the development bridge really holds, at the ids it really holds them under.
V1_COMMANDS = ((2, "probe_2"), (4, "light"))

# Fixed so the entity fixture can attach each 0.1 entity to the right subentry. Home Assistant
# generates these as ULIDs, but never parses them, so a readable fixture value is clearer here
# than a realistic-looking one copied from somebody's installation.
V1_SUBENTRY_IDS = {2: "v1-command-subentry-2", 4: "v1-command-subentry-4"}


def _v1_entry(hass: HomeAssistant, esphome_entry_id: str) -> MockConfigEntry:
    """A config entry exactly as 0.1 left it: schema version 1, one subentry per command."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        title="RF Bridge",
        version=1,
        minor_version=1,
        data={
            CONF_ESPHOME_ENTRY_ID: esphome_entry_id,
            CONF_ACTION_PREFIX: "rf_",
        },
        subentries_data=[
            {
                "subentry_id": V1_SUBENTRY_IDS[command_id],
                "data": {"command_id": command_id},
                "subentry_type": SUBENTRY_TYPE_COMMAND,
                "title": name,
                "unique_id": f"{BRIDGE_ID}_{command_id}",
            }
            for command_id, name in V1_COMMANDS
        ],
    )
    entry.add_to_hass(hass)
    return entry


def _v1_entities(hass: HomeAssistant, entry: MockConfigEntry) -> dict[int, str]:
    """The entity registry records 0.1 would have left behind, one per command subentry."""
    registry = er.async_get(hass)
    created: dict[int, str] = {}
    for command_id, name in V1_COMMANDS:
        record = registry.async_get_or_create(
            "button",
            DOMAIN,
            f"{BRIDGE_ID}_{command_id}",
            config_entry=entry,
            config_subentry_id=V1_SUBENTRY_IDS[command_id],
            original_name=name,
            original_icon="mdi:remote",
            suggested_object_id=name,
        )
        created[command_id] = record.entity_id
    return created


@pytest.fixture
def bridge(hass: HomeAssistant) -> FakeBridge:
    """A bridge already holding the two commands a 0.1 install would have."""
    fake = FakeBridge(hass)
    fake.commands = {command_id: name for command_id, name in V1_COMMANDS}
    fake.next_command_id = 5
    fake.revision = 5
    return fake


async def test_upgrade_preserves_every_command_entity(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """The whole point: nothing a user built on top of 0.1's entities breaks."""
    esphome_entry = async_add_esphome_entry(hass)
    entry = _v1_entry(hass, esphome_entry.entry_id)
    entity_ids = _v1_entities(hass, entry)

    registry = er.async_get(hass)
    # Customisations a real installation would carry: a renamed entity, a chosen icon, an area
    # set on the entity rather than inherited, and an entity id the user changed by hand.
    registry.async_update_entity(
        entity_ids[4], name="Ceiling light", icon="mdi:ceiling-light"
    )
    registry.async_update_entity(entity_ids[4], new_entity_id="button.bedroom_ceiling")
    entity_ids[4] = "button.bedroom_ceiling"
    registry.async_update_entity(entity_ids[2], area_id=None)
    before = {
        command_id: registry.async_get(entity_id)
        for command_id, entity_id in entity_ids.items()
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    # The schema moved forward exactly once.
    assert entry.version == 2
    assert entry.minor_version == 1

    # No command subentry is left, and no target was invented in their place.
    assert not entry.subentries

    for command_id, entity_id in entity_ids.items():
        after = registry.async_get(entity_id)
        assert after is not None, f"command {command_id} lost its entity"
        assert after.id == before[command_id].id, "the registry record was replaced"
        assert after.unique_id == f"{BRIDGE_ID}_{command_id}"
        assert after.config_entry_id == entry.entry_id
        # Unassigned: no subentry, and no device invented to hold it.
        assert after.config_subentry_id is None
        assert after.device_id is None
        assert assigned_target(entry, command_id) is None

    # The customisations came through untouched.
    assert registry.async_get("button.bedroom_ceiling").name == "Ceiling light"
    assert registry.async_get("button.bedroom_ceiling").icon == "mdi:ceiling-light"

    # And the buttons are live again, under the entity ids they already had.
    for entity_id in entity_ids.values():
        assert hass.states.get(entity_id) is not None


async def test_upgrade_makes_no_rf_mutation(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """An upgrade rearranges Home Assistant. It must not touch the bridge."""
    esphome_entry = async_add_esphome_entry(hass)
    entry = _v1_entry(hass, esphome_entry.entry_id)
    _v1_entities(hass, entry)
    revision_before = bridge.revision

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert bridge.mutations == [], "the upgrade wrote to the bridge"
    assert bridge.revision == revision_before
    assert bridge.commands == dict(V1_COMMANDS)
    assert bridge.next_command_id == 5


async def test_upgrade_is_idempotent_across_reloads(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """Running it again finds nothing to do, and finds nothing to break."""
    esphome_entry = async_add_esphome_entry(hass)
    entry = _v1_entry(hass, esphome_entry.entry_id)
    entity_ids = _v1_entities(hass, entry)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    record_ids = {
        command_id: registry.async_get(entity_id).id
        for command_id, entity_id in entity_ids.items()
    }

    for _ in range(2):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 2
    assert not entry.subentries
    assert bridge.mutations == []
    for command_id, entity_id in entity_ids.items():
        record = registry.async_get(entity_id)
        assert record is not None
        assert record.id == record_ids[command_id]
        assert record.config_subentry_id is None
    # No duplicate entities appeared for the same commands.
    ours = [
        record
        for record in er.async_entries_for_config_entry(registry, entry.entry_id)
        if record.domain == "button"
    ]
    assert len(ours) == len(V1_COMMANDS) + 1, "expected the commands plus Cancel learning"


async def test_upgrade_leaves_no_command_metadata_behind(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """Unassigned is the absence of metadata, not an entry that says so."""
    esphome_entry = async_add_esphome_entry(hass)
    entry = _v1_entry(hass, esphome_entry.entry_id)
    _v1_entities(hass, entry)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert command_meta(entry) == {}


async def test_a_downgraded_entry_is_refused_rather_than_rewritten(
    hass: HomeAssistant, bridge: FakeBridge
) -> None:
    """An entry from a future version is left alone for that version to read again."""
    esphome_entry = async_add_esphome_entry(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=BRIDGE_ID,
        title="RF Bridge",
        version=99,
        minor_version=1,
        data={CONF_ESPHOME_ENTRY_ID: esphome_entry.entry_id, CONF_ACTION_PREFIX: "rf_"},
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 99, "the entry was rewritten on the way down"
    assert bridge.mutations == []
